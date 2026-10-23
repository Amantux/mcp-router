"""Admin API: /api/v1/config/snapshot (export/import for backup/migration).

Export: GET /api/v1/config/snapshot returns a versioned JSON snapshot of
servers, principals, policy rules, skill sources, and server env variable NAMES.
Never includes credential VALUES or agent key material.

Import: POST /api/v1/config/snapshot/import restores a snapshot into an empty
or existing stack with upsert semantics. Newly created principals get fresh keys
returned exactly once in the response.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from mcprouter.api.deps import session_factory
from mcprouter.api.deps_auth import require_admin
from mcprouter.discovery.credentials import env_names
from mcprouter.discovery.registry import ServerRegistration, register_server
from mcprouter.generation import bump_catalog
from mcprouter.mcpclient.targets import TRANSPORTS, Transport
from mcprouter.models import (
    AgentPrincipal,
    MCPServerRecord,
    PolicyRule,
    SkillSourceRecord,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/config", tags=["config"], dependencies=[Depends(require_admin)])

SNAPSHOT_VERSION = 1


def _validate_transport(transport: str) -> Transport:
    if transport not in TRANSPORTS:
        raise ValueError(f"invalid transport: {transport}")
    return transport


# ------------------------------------------------------------------ export schemas


class ServerSnapshot(BaseModel):
    """Export shape for a server (no credential values)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    transport: str
    endpoint: str | None
    enabled: bool


class PrincipalSnapshot(BaseModel):
    """Export shape for a principal (no key material)."""

    agent_id: str
    enabled: bool
    max_tools: int
    max_servers: int | None
    max_skills: int


class PolicyRuleSnapshot(BaseModel):
    """Export shape for a policy rule."""

    agent_id: str
    server_id: str | None
    tool_name: str | None
    max_operation: str
    requires_approval: bool


class SkillSourceSnapshot(BaseModel):
    """Export shape for a skill source."""

    name: str
    kind: str
    location: str
    git_ref: str | None
    enabled: bool


class SnapshotOut(BaseModel):
    """Export response payload."""

    snapshot_version: int = Field(default=SNAPSHOT_VERSION)
    exported_at: str
    servers: list[ServerSnapshot]
    principals: list[PrincipalSnapshot]
    policy_rules: list[PolicyRuleSnapshot]
    skill_sources: list[SkillSourceSnapshot]
    server_env_names: dict[str, list[str]]


# ------------------------------------------------------------------ import schemas


class ServerImport(BaseModel):
    """Import shape for a server."""

    name: str
    transport: str
    endpoint: str | None = None
    command: list[str] | None = None
    enabled: bool = True


class PrincipalImport(BaseModel):
    """Import shape for a principal."""

    agent_id: str
    enabled: bool = True
    max_tools: int = 8
    max_servers: int | None = None
    max_skills: int = 3


class PolicyRuleImport(BaseModel):
    """Import shape for a policy rule."""

    agent_id: str
    server_id: str | None = None
    tool_name: str | None = None
    max_operation: str = "read"
    requires_approval: bool = False


class SkillSourceImport(BaseModel):
    """Import shape for a skill source."""

    name: str
    kind: str
    location: str
    git_ref: str | None = None
    enabled: bool = True


class SnapshotIn(BaseModel):
    """Import request payload."""

    snapshot_version: int
    servers: list[ServerImport] = Field(default_factory=list)
    principals: list[PrincipalImport] = Field(default_factory=list)
    policy_rules: list[PolicyRuleImport] = Field(default_factory=list)
    skill_sources: list[SkillSourceImport] = Field(default_factory=list)


class ImportReportItem(BaseModel):
    """Single item in an import report."""

    name: str
    status: str  # created|skipped
    reason: str | None = None


class ImportReport(BaseModel):
    """Import response payload."""

    servers: list[ImportReportItem]
    principals: list[ImportReportItem]
    policy_rules: list[ImportReportItem]
    skill_sources: list[ImportReportItem]
    api_keys: dict[str, str]  # agent_id -> newly generated key


# ------------------------------------------------------------------ helpers


def _server_to_snapshot(s: MCPServerRecord) -> ServerSnapshot:
    return ServerSnapshot(
        id=s.id,
        name=s.name,
        transport=s.transport,
        endpoint=s.endpoint,
        enabled=s.enabled,
    )


def _principal_to_snapshot(p: AgentPrincipal) -> PrincipalSnapshot:
    return PrincipalSnapshot(
        agent_id=p.agent_id,
        enabled=p.enabled,
        max_tools=p.max_tools,
        max_servers=p.max_servers,
        max_skills=p.max_skills,
    )


def _rule_to_snapshot(r: PolicyRule) -> PolicyRuleSnapshot:
    return PolicyRuleSnapshot(
        agent_id=r.agent_id,
        server_id=r.server_id,
        tool_name=r.tool_name,
        max_operation=r.max_operation,
        requires_approval=r.requires_approval,
    )


def _skill_source_to_snapshot(s: SkillSourceRecord) -> SkillSourceSnapshot:
    return SkillSourceSnapshot(
        name=s.name,
        kind=s.kind,
        location=s.location,
        git_ref=s.git_ref,
        enabled=s.enabled,
    )


# ------------------------------------------------------------------ export endpoint


@router.get("/snapshot", response_model=SnapshotOut)
def export_snapshot(request: Request) -> SnapshotOut:
    """Export operator-visible configuration as versioned JSON.

    Never includes: server credential VALUES, agent key material.
    """
    factory = session_factory(request)

    with factory() as s:
        servers = s.scalars(select(MCPServerRecord).order_by(MCPServerRecord.name)).all()
        principals = s.scalars(select(AgentPrincipal).order_by(AgentPrincipal.agent_id)).all()
        policy_rules = s.scalars(select(PolicyRule).order_by(PolicyRule.agent_id)).all()
        skill_sources = s.scalars(select(SkillSourceRecord).order_by(SkillSourceRecord.name)).all()

        # Collect env variable NAMES per server (never values)
        server_ids = [s.id for s in servers]
        env_names_map: dict[str, list[str]] = {}
        if server_ids:
            env_names_map = env_names(s, server_ids)

    return SnapshotOut(
        snapshot_version=SNAPSHOT_VERSION,
        exported_at=datetime.now(UTC).isoformat(),
        servers=[_server_to_snapshot(s) for s in servers],
        principals=[_principal_to_snapshot(p) for p in principals],
        policy_rules=[_rule_to_snapshot(r) for r in policy_rules],
        skill_sources=[_skill_source_to_snapshot(ss) for ss in skill_sources],
        server_env_names=env_names_map,
    )


# ------------------------------------------------------------------ import endpoint


def _import_servers(session: Session, servers: list[ServerImport]) -> list[ImportReportItem]:
    """Import servers with upsert semantics (skip duplicates)."""
    results: list[ImportReportItem] = []

    for item in servers:
        # Check if server with this name already exists
        existing = session.scalar(
            select(MCPServerRecord.id).where(MCPServerRecord.name == item.name)
        )
        if existing:
            results.append(
                ImportReportItem(name=item.name, status="skipped", reason="server already exists")
            )
            continue

        # Create new server
        reg = ServerRegistration(
            name=item.name,
            transport=_validate_transport(item.transport),
            endpoint=item.endpoint,
            command=tuple(item.command) if item.command else None,
            env={},
            enabled=item.enabled,
        )
        register_server(session, reg)
        session.flush()
        results.append(ImportReportItem(name=item.name, status="created"))

    return results


def _import_principals(
    session: Session, principals: list[PrincipalImport]
) -> tuple[list[ImportReportItem], dict[str, str]]:
    """Import principals with create-only semantics (skip duplicates).

    Returns (report_items, api_keys_dict).
    """
    from mcprouter.auth.keys import generate_key, hash_key

    results: list[ImportReportItem] = []
    api_keys: dict[str, str] = {}

    for item in principals:
        # Check if principal with this agent_id already exists
        existing = session.scalar(
            select(AgentPrincipal.id).where(AgentPrincipal.agent_id == item.agent_id)
        )
        if existing:
            results.append(
                ImportReportItem(
                    name=item.agent_id, status="skipped", reason="principal already exists"
                )
            )
            continue

        # Create new principal with fresh key
        key = generate_key()
        p = AgentPrincipal(
            agent_id=item.agent_id,
            key_hash=hash_key(key),
            enabled=item.enabled,
            max_tools=item.max_tools,
            max_servers=item.max_servers,
            max_skills=item.max_skills,
        )
        session.add(p)
        try:
            session.flush()
            results.append(ImportReportItem(name=item.agent_id, status="created"))
            api_keys[item.agent_id] = key
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            results.append(
                ImportReportItem(
                    name=item.agent_id,
                    status="skipped",
                    reason=f"failed to create: {exc}",
                )
            )

    return results, api_keys


def _import_policy_rules(session: Session, rules: list[PolicyRuleImport]) -> list[ImportReportItem]:
    """Import policy rules with upsert semantics (skip duplicates)."""
    results: list[ImportReportItem] = []

    for item in rules:
        # Check if rule already exists (same agent_id, server_id, tool_name)
        existing = session.scalar(
            select(PolicyRule.id).where(
                PolicyRule.agent_id == item.agent_id,
                PolicyRule.server_id == item.server_id,
                PolicyRule.tool_name == item.tool_name,
            )
        )
        if existing:
            results.append(
                ImportReportItem(
                    name=f"{item.agent_id}/{item.server_id or '*'}/{item.tool_name or '*'}",
                    status="skipped",
                    reason="rule already exists",
                )
            )
            continue

        # Create new rule
        r = PolicyRule(
            agent_id=item.agent_id,
            server_id=item.server_id,
            tool_name=item.tool_name,
            max_operation=item.max_operation,
            requires_approval=item.requires_approval,
        )
        session.add(r)
        try:
            session.flush()
            results.append(
                ImportReportItem(
                    name=f"{item.agent_id}/{item.server_id or '*'}/{item.tool_name or '*'}",
                    status="created",
                )
            )
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            results.append(
                ImportReportItem(
                    name=f"{item.agent_id}/{item.server_id or '*'}/{item.tool_name or '*'}",
                    status="skipped",
                    reason=f"failed to create: {exc}",
                )
            )

    return results


def _import_skill_sources(
    session: Session, skill_sources: list[SkillSourceImport]
) -> list[ImportReportItem]:
    """Import skill sources with upsert semantics (skip duplicates)."""
    results: list[ImportReportItem] = []

    for item in skill_sources:
        # Check if skill source with this name already exists
        existing = session.scalar(
            select(SkillSourceRecord.id).where(SkillSourceRecord.name == item.name)
        )
        if existing:
            results.append(
                ImportReportItem(
                    name=item.name, status="skipped", reason="skill source already exists"
                )
            )
            continue

        # Create new skill source
        ss = SkillSourceRecord(
            name=item.name,
            kind=item.kind,
            location=item.location,
            git_ref=item.git_ref,
            enabled=item.enabled,
        )
        session.add(ss)
        try:
            session.flush()
            results.append(ImportReportItem(name=item.name, status="created"))
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            results.append(
                ImportReportItem(
                    name=item.name,
                    status="skipped",
                    reason=f"failed to create: {exc}",
                )
            )

    return results


@router.post("/snapshot/import", response_model=ImportReport)
def import_snapshot(body: SnapshotIn, request: Request) -> ImportReport:
    """Import a configuration snapshot.

    - Validates snapshot_version (400 on mismatch/missing).
    - Servers: upsert by name (skip duplicates).
    - Policy rules: upsert by composite key (skip duplicates).
    - Principals: create-only with fresh keys returned once.
    - Skill sources: upsert by name (skip duplicates).
    """
    # Validate snapshot version
    if body.snapshot_version != SNAPSHOT_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported snapshot version {body.snapshot_version}. "
            f"Supported version: {SNAPSHOT_VERSION}",
        )

    factory = session_factory(request)

    with factory() as s, s.begin():
        # Import servers
        server_results = _import_servers(s, body.servers)

        # Import principals (collect new keys)
        principal_results, api_keys = _import_principals(s, body.principals)

        # Import policy rules
        rule_results = _import_policy_rules(s, body.policy_rules)

        # Import skill sources
        skill_source_results = _import_skill_sources(s, body.skill_sources)

        # Bump catalog cache
        bump_catalog()

    return ImportReport(
        servers=server_results,
        principals=principal_results,
        policy_rules=rule_results,
        skill_sources=skill_source_results,
        api_keys=api_keys,
    )
