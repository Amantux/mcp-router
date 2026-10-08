"""Catalog lifecycle: what runs after a discovery sync changed the catalog
(integration gap 1).

Without this, nothing in production classified or embedded newly discovered
tools: they stayed `operation=unknown` (policy treats that as execute, so a
read-only agent saw nothing) and unembedded (routing's vector leg was empty).

`make_post_sync_hook` builds the hook `DiscoveryService` runs after any sync
that changed the catalog — both the manual refresh endpoint and the
background `SyncLoop` (via `sync_all`, once per pass):

1. Auto-classify the changed tools (added / schema / metadata / restored)
   plus any never-classified backlog, through `registry.catalog.auto_classify`
   -> `apply_auto_classification`, whose UPDATE carries the
   `classification_reviewed = false` guard: a human-reviewed classification is
   never overwritten.
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

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery.sync import PostSyncHook, SyncReport
from mcprouter.generation import bump_catalog
from mcprouter.inference.pipeline import embed_pending_tools
from mcprouter.interfaces import EmbeddingBackend
from mcprouter.models import MCPToolRecord
from mcprouter.registry.catalog import auto_classify
from mcprouter.registry.classify import RuleBasedClassifier, ToolClassifier

log = logging.getLogger(__name__)


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
                run = auto_classify(s, clf, tool_ids=ids) if ids else None
                s.commit()
            if run is not None:
                log.info(
                    "post-sync classify classified=%d skipped_reviewed=%d",
                    run.classified,
                    run.skipped_reviewed,
                )
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
