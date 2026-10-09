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

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.models import SkillRecord, SkillSourceRecord
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
            out = []
            for sid in ids:
                sk = s.get(SkillRecord, sid)
                if sk is None or not sk.enabled or not sk.available:
                    continue
                src = sk.source
                if not src.enabled:
                    continue
                s.expunge(sk)
                s.expunge(src)
                out.append((sk, src))
            return out

    def resolve(
        self, name_or_id: str, routed_ids: Iterable[str]
    ) -> tuple[SkillRecord, SkillSourceRecord]:
        for sk, src in self.load_routed(routed_ids):
            if name_or_id in (sk.id, prompt_name(src.name, sk.name)):
                return sk, src
        raise SkillAccessError("not_found", "Unknown skill.")

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
        sk, src = self.resolve(name_or_id, routed_ids)
        self._gate(agent_id, sk, src, route_request_id, initiated_by)
        body = read_body(sk, self._body_max)
        rid = self._manager.record_skill_activation(
            agent_id, sk.id, "ok", "activated", route_request_id, initiated_by
        )
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
        sk, src = self.resolve(name_or_id, routed_ids)
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
        self._manager.record_skill_activation(
            agent_id, sk.id, "ok", f"resource: {content.path}", route_request_id, initiated_by
        )
        return content
