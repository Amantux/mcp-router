"""Skill exposure core (wave-4 S3) — the ONE implementation behind MCP
prompts/get, resources/read, the router.activate_skill / router.read_skill_resource
meta-tools, and the REST activate/resource/bundle endpoints.

Order on every activation/read (each step mutation-checked in tests):
  1. visibility — the skill must be in the caller's routed set (another agent's
     routed skill is indistinguishable from a nonexistent one)
  2. rate limit per principal (shared sliding-window limiter)
  3. policy RE-CHECK at activation (defense in depth; routing already filtered)
  4. audit row written (ExecutionRecord resource_kind="skill") ...
  5. ... and only then the body / resource bytes are returned.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.models import SkillRecord, SkillSourceRecord
from mcprouter.skills.bundle import BundleError, _build_bundle
from mcprouter.skills.serve import (
    ResourceContent,
    SkillFiles,
    SkillServeError,
    read_body,
    source_root,
)


class SkillPolicy(Protocol):
    """SEAM for S2's kind-aware policy engine (PolicyRule.resource_kind="skill").
    Returns (allowed, curated_reason). The integrator wires S2's engine here."""

    def check(
        self, agent_id: str, skill: SkillRecord, source: SkillSourceRecord
    ) -> tuple[bool, str]: ...


class DenyAllSkillPolicy:
    """Fail-closed default until S2's engine is wired."""

    def check(
        self, agent_id: str, skill: SkillRecord, source: SkillSourceRecord
    ) -> tuple[bool, str]:
        return False, "no skill policy configured"


class SkillAccessError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # not_found | denied | rate_limited | invalid_path | ...
        self.message = message


@dataclass(frozen=True)
class Activation:
    skill_id: str
    name: str
    body: str
    resources: list[dict[str, object]]
    record_id: str


def prompt_name(source_name: str, skill_name: str) -> str:
    return f"{source_name}/{skill_name}"


def resource_uri(source_name: str, skill_name: str, path: str) -> str:
    return f"skill://{source_name}/{skill_name}/{path}"


class SkillExposure:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        manager: ExecutionManager,
        policy: SkillPolicy,
        limiter: SlidingWindowLimiter,
        *,
        cache_dir: str,
        body_max_bytes: int,
        resource_max_bytes: int,
    ) -> None:
        self._factory = session_factory
        self._manager = manager
        self._policy = policy
        self._limiter = limiter
        self._cache_dir = cache_dir
        self._body_max = body_max_bytes
        self._res_max = resource_max_bytes

    # ------------------------------------------------------------ listing
    def load_routed(self, routed_ids: Iterable[str]) -> list[tuple[SkillRecord, SkillSourceRecord]]:
        ids = list(dict.fromkeys(routed_ids))
        if not ids:
            return []  # skills are opt-in via routing: no route => none exposed
        with self._factory() as s:
            # ONE query (skill JOIN source) regardless of how many ids are routed.
            stmt = (
                select(SkillRecord)
                .where(SkillRecord.id.in_(ids))
                .options(joinedload(SkillRecord.source))
            )
            found = {sk.id: sk for sk in s.scalars(stmt).unique()}
            out = []
            for sid in ids:  # preserve routed order
                sk = found.get(sid)
                if sk is None or not sk.enabled or not sk.available:
                    continue
                src = sk.source
                if not src.enabled:
                    continue
                s.expunge(sk)
                if src in s:  # sources are shared between skills
                    s.expunge(src)
                out.append((sk, src))
            return out

    def resolve(
        self, name_or_id: str, routed_ids: Iterable[str]
    ) -> tuple[SkillRecord, SkillSourceRecord]:
        # A prompt name is exactly "<source>/<skill>". Skill names cannot hold
        # "/" (spec regex) but a source name could, which would make the split
        # ambiguous -- refuse rather than guess. Ingest should reject "/" in
        # source names (integrator note in docs/history/INTEGRATION_NOTES-wave4-exposure.md).
        if name_or_id.count("/") > 1:
            raise SkillAccessError("invalid_name", "Invalid skill name.")
        for sk, src in self.load_routed(routed_ids):
            if name_or_id == sk.id or (
                "/" not in src.name and name_or_id == prompt_name(src.name, sk.name)
            ):
                return sk, src
        raise SkillAccessError("not_found", "Unknown skill.")

    def _resolve_audited(
        self,
        agent_id: str,
        name_or_id: str,
        routed_ids: Iterable[str],
        route_request_id: str | None,
        initiated_by: str | None,
    ) -> tuple[SkillRecord, SkillSourceRecord]:
        """resolve(), auditing an unknown/unrouted access as outcome "denied",
        detail "not routed" (owner decision). No skill id is attributed: the
        ref is caller-controlled and may not name a real skill. The caller
        still sees the same curated 404."""
        try:
            return self.resolve(name_or_id, routed_ids)
        except SkillAccessError as exc:
            if exc.code == "not_found":
                self._manager.record_skill_activation(
                    agent_id, None, "denied", "not routed", route_request_id, initiated_by
                )
            raise

    def _internal(
        self,
        agent_id: str,
        skill_id: str,
        exc: Exception,
        route_request_id: str | None,
        initiated_by: str | None,
    ) -> SkillAccessError:
        """Audit an untyped failure (class name only) and curate it."""
        try:
            self._manager.record_skill_activation(
                agent_id,
                skill_id,
                "error",
                f"internal: {type(exc).__name__}",
                route_request_id,
                initiated_by,
            )
        except Exception:  # noqa: BLE001,S110 -- audit is best-effort on a failure path
            pass
        return SkillAccessError("internal", "Internal error while serving the skill.")

    # ------------------------------------------------------------- gating
    def _gate(
        self,
        agent_id: str,
        sk: SkillRecord,
        src: SkillSourceRecord,
        route_request_id: str | None,
        initiated_by: str | None,
    ) -> None:
        if not self._limiter.try_acquire(f"skill:{agent_id}"):
            self._manager.record_skill_activation(
                agent_id, sk.id, "rate_limited", "rate limited", route_request_id, initiated_by
            )
            raise SkillAccessError("rate_limited", "Too many skill activations; retry later.")
        allowed, reason = self._policy.check(agent_id, sk, src)
        if not allowed:
            self._manager.record_skill_activation(
                agent_id, sk.id, "denied", f"policy: {reason}", route_request_id, initiated_by
            )
            raise SkillAccessError("denied", "Skill activation denied by policy.")

    def activate(
        self,
        agent_id: str,
        name_or_id: str,
        routed_ids: Iterable[str],
        route_request_id: str | None = None,
        initiated_by: str | None = None,
    ) -> Activation:
        sk, src = self._resolve_audited(
            agent_id, name_or_id, routed_ids, route_request_id, initiated_by
        )
        self._gate(agent_id, sk, src, route_request_id, initiated_by)
        try:
            body = read_body(sk, self._body_max)
            rid = self._manager.record_skill_activation(
                agent_id, sk.id, "ok", "activated", route_request_id, initiated_by
            )
        except SkillAccessError:
            raise
        except Exception as exc:  # noqa: BLE001 -- curated: class name only
            raise self._internal(agent_id, sk.id, exc, route_request_id, initiated_by) from None
        manifest = [
            {"path": e.get("path"), "size": e.get("size"), "kind": e.get("kind")}
            for e in sk.resource_manifest or []
        ]
        return Activation(sk.id, prompt_name(src.name, sk.name), body, manifest, rid)

    def read_resource(
        self,
        agent_id: str,
        name_or_id: str,
        path: str,
        routed_ids: Iterable[str],
        route_request_id: str | None = None,
        initiated_by: str | None = None,
    ) -> ResourceContent:
        sk, src = self._resolve_audited(
            agent_id, name_or_id, routed_ids, route_request_id, initiated_by
        )
        self._gate(agent_id, sk, src, route_request_id, initiated_by)
        files = SkillFiles(source_root(src, self._cache_dir), self._res_max)
        try:
            content = files.read_resource(sk, path)
        except SkillServeError as exc:
            self._manager.record_skill_activation(
                agent_id,
                sk.id,
                "error",
                f"resource refused: {exc.code}",
                route_request_id,
                initiated_by,
            )
            raise SkillAccessError(exc.code, exc.message) from None
        except Exception as exc:  # noqa: BLE001 -- curated: class name only
            raise self._internal(agent_id, sk.id, exc, route_request_id, initiated_by) from None
        # outcome "read", not "ok": only body activations bump activation_count
        # (the manager bumps on outcome == "ok").
        self._manager.record_skill_activation(
            agent_id, sk.id, "read", f"resource: {content.path}", route_request_id, initiated_by
        )
        return content

    def bundle(
        self,
        agent_id: str,
        routed_ids: Iterable[str],
        route_request_id: str | None = None,
        initiated_by: str | None = None,
    ) -> tuple[bytes, list[str]]:
        """Zip of the caller's routed skills: visibility -> limiter (one token per
        bundle) -> policy per skill (denied ones are audited and left out) ->
        one audit row (outcome "bundle") per included skill -- delivery in a
        bundle is not an activation, so activation_count is never bumped ->
        bytes. Returns (zip bytes, skipped resource paths)."""
        routed = self.load_routed(routed_ids)  # 1. visibility
        if not routed:
            raise SkillAccessError("not_found", "No skills are routed to this agent.")
        if not self._limiter.try_acquire(f"skill:{agent_id}"):  # 2. rate limit
            # ONE row per refused bundle (not per routed skill): a hammering
            # caller must not amplify writes by the size of its routing.
            self._manager.record_skill_activation(
                agent_id, routed[0][0].id, "rate_limited", "bundle", route_request_id, initiated_by
            )
            raise SkillAccessError("rate_limited", "Too many skill activations; retry later.")
        allowed = []
        for sk, src in routed:  # 3. policy re-check
            ok, reason = self._policy.check(agent_id, sk, src)
            if ok:
                allowed.append((sk, SkillFiles(source_root(src, self._cache_dir), self._res_max)))
            else:
                self._manager.record_skill_activation(
                    agent_id,
                    sk.id,
                    "denied",
                    f"bundle policy: {reason}",
                    route_request_id,
                    initiated_by,
                )
        if not allowed:
            raise SkillAccessError("denied", "Skill activation denied by policy.")
        try:
            data, skipped = _build_bundle(allowed)
        except BundleError as exc:
            self._manager.record_skill_activation(
                agent_id,
                allowed[0][0].id,
                "error",
                f"bundle: {exc.code}",
                route_request_id,
                initiated_by,
            )
            raise SkillAccessError(exc.code, exc.message) from None
        except Exception as exc:  # noqa: BLE001 -- curated: class name only
            raise self._internal(
                agent_id, allowed[0][0].id, exc, route_request_id, initiated_by
            ) from None
        for sk, _ in allowed:  # 4. audit before the bytes leave
            self._manager.record_skill_activation(
                agent_id, sk.id, "bundle", "bundle", route_request_id, initiated_by
            )
        return data, skipped
