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
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery.sync import PostSyncHook, SyncReport
from mcprouter.generation import bump_catalog
from mcprouter.inference.pipeline import embed_pending_tools
from mcprouter.interfaces import EmbeddingBackend
from mcprouter.models import MCPToolRecord
from mcprouter.registry.catalog import apply_auto_classification
from mcprouter.registry.classify import RuleBasedClassifier, ToolClassifier
from mcprouter.settings import Settings

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


# --- wave-4: skill-source post-sync (classify changed skills, embed pending) ---


def skill_content_snapshot(session: Session, source_id: str) -> dict[str, str]:
    """{skill_id: content_hash} for a source — taken BEFORE a sync so the hook
    can tell new / content-changed skills apart from untouched ones."""
    from sqlalchemy import select

    from mcprouter.models import SkillRecord

    rows = session.execute(
        select(SkillRecord.id, SkillRecord.content_hash).where(SkillRecord.source_id == source_id)
    ).all()
    return {r[0]: r[1] for r in rows}


def run_skill_post_sync(
    session_factory: sessionmaker[Session],
    embedder: EmbeddingBackend,
    source_id: str,
    before: dict[str, str],
    *,
    embed_batch_size: int = 32,
) -> None:
    """Best effort (the sync already committed): classify skills that are new,
    content-changed, or never auto-classified, then embed pending skills."""
    from mcprouter.inference.pipeline import embed_pending_skills
    from mcprouter.models import SkillRecord
    from mcprouter.registry.classify import apply_skill_classification, classify_skill

    try:
        with session_factory() as s:
            from sqlalchemy import select

            n = 0
            for sk in s.scalars(select(SkillRecord).where(SkillRecord.source_id == source_id)):
                changed = sk.id in before and before[sk.id] != sk.content_hash
                if sk.id in before and not changed and sk.classification_source is not None:
                    continue
                c = classify_skill(
                    sk.name,
                    sk.description,
                    sk.body,
                    has_scripts=sk.has_scripts,
                    allowed_tools=list(sk.allowed_tools or []),
                )
                n += int(apply_skill_classification(s, sk.id, c, content_changed=changed))
            s.commit()
        log.info("skill post-sync classify source=%s classified=%d", source_id, n)
    except Exception as exc:  # noqa: BLE001 — best effort; the sync already succeeded
        log.warning("skill post-sync classification failed: %s", type(exc).__name__)
    try:
        with session_factory() as s:
            rep = embed_pending_skills(s, embedder, batch_size=embed_batch_size)
            s.commit()
        log.info("skill post-sync embed embedded=%d", rep.embedded)
    except Exception as exc:  # noqa: BLE001 — best effort; the sync already succeeded
        log.warning("skill post-sync embedding failed: %s", type(exc).__name__)
    bump_catalog()


def run_due_skill_syncs(
    factory: sessionmaker[Session],
    settings: Settings,
    post_sync: Callable[[str, dict[str, str]], None] | None,
    now: datetime,
    attempts: dict[str, datetime],
) -> list[str]:
    """Scheduled skill-source tick: sync every ENABLED source whose
    ``sync_interval_s`` has elapsed since its last success (``last_synced_at``)
    or last attempt (``attempts``, so a failing git source isn't hammered every
    tick). Returns the ids attempted. One bad source never blocks the rest."""
    from mcprouter.models import SkillSourceRecord
    from mcprouter.skills.sources import run_sync

    with factory() as s:
        rows = s.execute(
            select(
                SkillSourceRecord.id,
                SkillSourceRecord.last_synced_at,
                SkillSourceRecord.sync_interval_s,
            ).where(SkillSourceRecord.enabled.is_(True))
        ).all()
    due: list[str] = []
    for sid, last, interval in rows:
        marks = [m for m in (last, attempts.get(sid)) if m is not None]
        marks = [m if m.tzinfo else m.replace(tzinfo=UTC) for m in marks]
        if not marks or (now - max(marks)).total_seconds() >= interval:
            due.append(sid)
    for sid in due:
        attempts[sid] = now
        try:
            with factory() as s:
                rec = s.get(SkillSourceRecord, sid)
                if rec is None:
                    continue
                before = skill_content_snapshot(s, sid)
                run_sync(s, rec, settings)
                s.commit()
            if post_sync is not None:
                post_sync(sid, before)
        except Exception as exc:  # noqa: BLE001 — one bad source must not stop the tick
            log.warning("scheduled skill sync failed: %s", type(exc).__name__)
    return due
