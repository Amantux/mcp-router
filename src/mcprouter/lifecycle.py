"""Catalog lifecycle: what runs after a discovery sync changed the catalog
(integration gap 1).

Without this, nothing in production classified or embedded newly discovered
tools: they stayed `operation=unknown` (policy treats that as execute, so a
read-only agent saw nothing) and unembedded (routing's vector leg was empty).

`make_post_sync_hook` builds the hook `DiscoveryService` runs after any sync
that changed the catalog — both the manual refresh endpoint and the
background `SyncLoop` (via `sync_all`, once per pass):

1. Auto-classify the changed tools (added / schema / metadata / restored)
   plus any never-classified backlog, through
   `registry.catalog.apply_auto_classification`, whose UPDATE carries the
   `classification_reviewed = false` guard: a human-reviewed classification is
   never overwritten. Unattended RE-classification never widens access: an
   operation set by an earlier automatic run may only move toward
   execute/unknown (upstream metadata is untrusted).
2. `embed_pending_tools` with the configured embedding backend (re-embeds only
   what is missing / changed / from another backend).
3. Bump the catalog generation so cached routes see the new state.

Each step commits on its own and is best effort: a classifier or embedding
failure is logged (type name only) and never fails the refresh — the sync
itself already succeeded and its catalog rows are committed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery.sync import PostSyncHook, SyncReport
from mcprouter.generation import bump_catalog
from mcprouter.inference.pipeline import embed_pending_tools
from mcprouter.interfaces import EmbeddingBackend
from mcprouter.models import MCPToolRecord
from mcprouter.registry.catalog import apply_auto_classification
from mcprouter.registry.classify import RuleBasedClassifier, ToolClassifier

log = logging.getLogger(__name__)


# Severity for the non-widening rule: unknown is the MOST restricted class
# (policy.engine treats anything unrecognised as execute, and above it here so
# unknown -> execute counts as no widening).
_SEVERITY = {"read": 0, "write": 1, "execute": 2}
_UNKNOWN_RANK = 3


def _rank(operation: str | None) -> int:
    return _SEVERITY.get(operation or "", _UNKNOWN_RANK)


def _classify_non_widening(s: Session, clf: ToolClassifier, tool_ids: list[str]) -> tuple[int, int]:
    """Unattended reclassification may only NARROW an operation that an
    earlier automatic classification set: upstream metadata is untrusted, so
    rewriting "Delete a ticket" to "Get a ticket" must not quietly make the
    tool readable by read-only agents. A tool never classified before
    (classification_source IS NULL) gets the classifier's answer, whatever it
    is. Reviewed tools are excluded by apply_auto_classification's guard.
    Returns (classified, kept_operation)."""
    rows = s.execute(
        select(
            MCPToolRecord.id,
            MCPToolRecord.name,
            MCPToolRecord.description,
            MCPToolRecord.input_schema,
            MCPToolRecord.operation,
            MCPToolRecord.classification_source,
        ).where(MCPToolRecord.id.in_(tool_ids))
    ).all()
    classified = kept = 0
    for tid, name, desc, schema, current, source in rows:
        c = clf.classify(name, desc or "", schema or {})
        if source is not None and _rank(c.operation) < _rank(current):
            c = replace(c, operation=current)  # would widen: keep the stricter class
            kept += 1
            log.info("post-sync classify kept stricter operation tool_id=%s", tid)
        if apply_auto_classification(s, tid, c, source=clf.name):
            classified += 1
    return classified, kept


def _tools_to_classify(s: Session, reports: Sequence[SyncReport]) -> list[str]:
    changed = [
        and_(MCPToolRecord.server_id == r.server_id, MCPToolRecord.name.in_(names))
        for r in reports
        if (names := [*r.added, *r.schema_changed, *r.metadata_changed, *r.restored])
    ]
    stmt = select(MCPToolRecord.id).where(
        MCPToolRecord.classification_reviewed.is_(False),
        # Changed tools, plus the backlog that predates this hook.
        or_(MCPToolRecord.classification_source.is_(None), *changed),
    )
    return list(s.scalars(stmt))


def make_post_sync_hook(
    session_factory: sessionmaker[Session],
    embedder: EmbeddingBackend,
    *,
    classifier: ToolClassifier | None = None,
    embed_batch_size: int = 32,
) -> PostSyncHook:
    clf: ToolClassifier = classifier or RuleBasedClassifier()

    def hook(reports: Sequence[SyncReport]) -> None:
        try:
            with session_factory() as s:
                ids = _tools_to_classify(s, reports)
                classified, kept = _classify_non_widening(s, clf, ids) if ids else (0, 0)
                s.commit()
            log.info("post-sync classify classified=%d kept_stricter=%d", classified, kept)
        except Exception as exc:  # noqa: BLE001 — best effort; the sync already succeeded
            log.warning("post-sync classification failed: %s", type(exc).__name__)
        try:
            with session_factory() as s:
                rep = embed_pending_tools(s, embedder, batch_size=embed_batch_size)
                s.commit()
            log.info(
                "post-sync embed backend=%s embedded=%d conflicts=%d",
                rep.backend,
                rep.embedded,
                rep.conflicts,
            )
        except Exception as exc:  # noqa: BLE001 — best effort; the sync already succeeded
            log.warning("post-sync embedding failed: %s", type(exc).__name__)
        bump_catalog()

    return hook
