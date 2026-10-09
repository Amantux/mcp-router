"""Wave-4 S3: activation gating + audit (DB). Mutation-checked: visibility,
policy re-check, audit-before-return, rate limit."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.gateway.skills import SkillAccessError, SkillExposure, prompt_name
from mcprouter.models import ExecutionRecord, SkillRecord, SkillSourceRecord

from .conftest import requires_db
from .test_execution_support import FakeInvoker

pytestmark = requires_db


class FakePolicy:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls = 0

    def check(
        self, agent_id: str, skill: SkillRecord, source: SkillSourceRecord
    ) -> tuple[bool, str]:
        self.calls += 1
        return self.allow, "rule r1"


def _seed(factory: sessionmaker[Session], root: Path) -> tuple[str, str]:
    sd = root / "pdf-tools"
    sd.mkdir(parents=True)
    (sd / "guide.md").write_text("# Guide")
    with factory() as s:
        src = SkillSourceRecord(name="local", kind="directory", location=str(root))
        s.add(src)
        s.flush()
        ids = []
        for n in ("pdf-tools", "other-skill"):
            sk = SkillRecord(
                source_id=src.id,
                name=n,
                description=f"{n} desc",
                body=f"BODY {n}",
                relative_path=n,
                resource_manifest=[
                    {
                        "path": "guide.md",
                        "size": 7,
                        "sha256": hashlib.sha256(b"# Guide").hexdigest(),
                    }
                ],
                content_hash="0" * 64,
                manifest_hash="0" * 64,
            )
            s.add(sk)
            s.flush()
            ids.append(sk.id)
        s.commit()
        return ids[0], ids[1]


def _exp(factory: sessionmaker[Session], policy: FakePolicy, limit: int = 100) -> SkillExposure:
    mgr = ExecutionManager(factory, FakeInvoker(), timeout_s=1.0, limiter=SlidingWindowLimiter(100))
    return SkillExposure(
        factory,
        mgr,
        policy,
        SlidingWindowLimiter(limit, 60.0),
        cache_dir="/nonexistent",
        body_max_bytes=65536,
        resource_max_bytes=1024,
    )


def _rows(factory: sessionmaker[Session]) -> list[ExecutionRecord]:
    with factory() as s:
        return list(s.scalars(select(ExecutionRecord)).all())


def test_activate_audits_and_counts(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, _ = _seed(db, tmp_path)
    act = _exp(db, FakePolicy()).activate("agent-a", "local/pdf-tools", [a], route_request_id="rr1")
    assert act.body == "BODY pdf-tools" and act.resources[0]["path"] == "guide.md"
    [row] = _rows(db)
    assert (row.id, row.resource_kind, row.skill_id, row.outcome) == (
        act.record_id,
        "skill",
        a,
        "ok",
    )
    assert row.route_request_id == "rr1" and row.agent_id == "agent-a" and row.tool_id is None
    with db() as s:
        assert s.get(SkillRecord, a).activation_count == 1  # type: ignore[union-attr]


def test_unrouted_skill_is_invisible(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, b = _seed(db, tmp_path)
    exp = _exp(db, FakePolicy())
    names = [prompt_name(src.name, sk.name) for sk, src in exp.load_routed([a])]
    assert names == ["local/pdf-tools"]
    for ref in ("local/other-skill", b):  # routed for someone else / by id
        with pytest.raises(SkillAccessError) as ei:
            exp.activate("agent-a", ref, [a])
        assert ei.value.code == "not_found"
    assert exp.load_routed([]) == []  # default exposure: none until routed
    assert _rows(db) == []


def test_policy_recheck_denies_and_audits(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, _ = _seed(db, tmp_path)
    pol = FakePolicy(allow=False)
    with pytest.raises(SkillAccessError) as ei:
        _exp(db, pol).activate("agent-a", a, [a])
    assert ei.value.code == "denied" and pol.calls == 1
    [row] = _rows(db)
    assert row.outcome == "denied" and "BODY" not in row.detail
    with db() as s:
        assert s.get(SkillRecord, a).activation_count == 0  # type: ignore[union-attr]


def test_audit_written_before_body_returned(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, _ = _seed(db, tmp_path)
    exp = _exp(db, FakePolicy())

    def boom(*args: object, **kw: object) -> str:
        raise RuntimeError("audit down")

    exp._manager.record_skill_activation = boom  # type: ignore[method-assign]
    with pytest.raises(SkillAccessError) as ei:
        exp.activate("agent-a", a, [a])  # no audit => no body
    assert ei.value.code == "internal" and "audit down" not in ei.value.message


def test_rate_limited(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, _ = _seed(db, tmp_path)
    exp = _exp(db, FakePolicy(), limit=1)
    exp.activate("agent-a", a, [a])
    with pytest.raises(SkillAccessError) as ei:
        exp.activate("agent-a", a, [a])
    assert ei.value.code == "rate_limited"
    assert sorted(r.outcome for r in _rows(db)) == ["ok", "rate_limited"]


def test_resource_read_gated_and_audited(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, _ = _seed(db, tmp_path)
    exp = _exp(db, FakePolicy())
    rc = exp.read_resource("agent-a", a, "guide.md", [a])
    assert rc.text == "# Guide"
    with pytest.raises(SkillAccessError) as ei:
        exp.read_resource("agent-a", a, "../pdf-tools/guide.md", [a])
    assert ei.value.code == "invalid_path"
    assert sorted((r.outcome, r.detail) for r in _rows(db)) == [
        ("error", "resource refused: invalid_path"),
        ("read", "resource: guide.md"),
    ]
    with pytest.raises(SkillAccessError):
        _exp(db, FakePolicy(allow=False)).read_resource("agent-a", a, "guide.md", [a])


def test_resource_read_does_not_bump_activation_count(
    db: sessionmaker[Session], tmp_path: Path
) -> None:
    a, _ = _seed(db, tmp_path)
    exp = _exp(db, FakePolicy())
    exp.read_resource("agent-a", a, "guide.md", [a])
    exp.read_resource("agent-a", a, "guide.md", [a])
    with db() as s:
        assert s.get(SkillRecord, a).activation_count in (0, None)  # type: ignore[union-attr]
    exp.activate("agent-a", a, [a])
    with db() as s:
        assert s.get(SkillRecord, a).activation_count == 1  # type: ignore[union-attr]


def test_load_routed_is_batched(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, b = _seed(db, tmp_path)
    exp = _exp(db, FakePolicy())
    engine = db.kw["bind"]
    stmts: list[str] = []

    def on_exec(*args: object) -> None:
        stmts.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", on_exec)
    try:
        got = exp.load_routed([b, a, "missing"])
    finally:
        event.remove(engine, "before_cursor_execute", on_exec)
    assert [sk.id for sk, _ in got] == [b, a]
    assert len([q for q in stmts if q.lstrip().upper().startswith("SELECT")]) <= 2


def _zip_names(data: bytes) -> set[str]:
    import io
    import zipfile

    return set(zipfile.ZipFile(io.BytesIO(data)).namelist())


def test_bundle_is_gated_and_audited(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, b = _seed(db, tmp_path)
    data, skipped = _exp(db, FakePolicy()).bundle("agent-a", [a])
    names = _zip_names(data)
    assert "pdf-tools/SKILL.md" in names and "pdf-tools/guide.md" in names
    assert not any(n.startswith("other-skill/") for n in names)  # visibility
    assert skipped == []
    assert [(r.skill_id, r.outcome, r.detail) for r in _rows(db)] == [(a, "ok", "bundle")]


def test_bundle_unrouted_is_not_found(db: sessionmaker[Session], tmp_path: Path) -> None:
    _seed(db, tmp_path)
    with pytest.raises(SkillAccessError) as ei:
        _exp(db, FakePolicy()).bundle("agent-a", [])
    assert ei.value.code == "not_found"


def test_bundle_policy_denied(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, _ = _seed(db, tmp_path)
    with pytest.raises(SkillAccessError) as ei:
        _exp(db, FakePolicy(allow=False)).bundle("agent-a", [a])
    assert ei.value.code == "denied"
    assert [r.outcome for r in _rows(db)] == ["denied"]


def test_bundle_rate_limited(db: sessionmaker[Session], tmp_path: Path) -> None:
    a, _ = _seed(db, tmp_path)
    exp = _exp(db, FakePolicy(), limit=1)
    exp.bundle("agent-a", [a])
    with pytest.raises(SkillAccessError) as ei:
        exp.bundle("agent-a", [a])
    assert ei.value.code == "rate_limited"
