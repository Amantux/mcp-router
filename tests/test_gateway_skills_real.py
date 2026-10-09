"""Gateway skill paths against the REAL SkillExposure + SkillFiles (no stubs):
traversal/escape attempts, untyped failures, clear(), "/"-ambiguous names."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import select, update

from mcprouter.gateway.skills import SkillAccessError
from mcprouter.models import ExecutionRecord, SkillRecord, SkillSourceRecord
from tests.test_gateway_mcp import _ctx, sec_db_fixture, world  # noqa: F401 — fixtures
from tests.test_gateway_skills import _route
from tests.test_skills_exposure_activation import FakePolicy, _exp, _seed

pytestmark = pytest.mark.anyio

SECRET = "/etc/very-secret-path"


_GUIDE_SHA = hashlib.sha256(b"# Guide").hexdigest()


@pytest.fixture()
def real(world: dict[str, Any], tmp_path: Path) -> dict[str, Any]:  # noqa: F811
    db = world["db"]
    root = tmp_path / "src"
    a, b = _seed(db, root)
    sd = root / "pdf-tools"
    outside = tmp_path / "outside.txt"
    outside.write_text("TOP SECRET")
    os.symlink(outside, sd / "escape.md")
    (sd / "big.md").write_text("x" * 2000)
    (sd / "unlisted.md").write_text("not in manifest")
    with db() as s:
        s.execute(
            update(SkillRecord)
            .where(SkillRecord.id == a)
            .values(
                resource_manifest=[
                    {"path": "guide.md", "size": 7, "kind": "text", "sha256": _GUIDE_SHA},
                    {"path": "escape.md", "size": 10, "kind": "text"},
                    {"path": "big.md", "size": 2000, "kind": "text"},
                ]
            )
        )
        s.commit()
    gw = world["gw"]
    gw._skills = _exp(db, FakePolicy())
    return {"gw": gw, "cat": world["cat"], "db": db, "a": a, "b": b}


def _rows(db: Any) -> list[ExecutionRecord]:
    with db() as s:
        return list(s.scalars(select(ExecutionRecord)).all())


async def _read(r: dict[str, Any], path: str, agent: str = "alice") -> Any:
    ctx = _ctx(r["gw"], r["cat"], agent)
    params = types.ReadResourceRequestParams(uri=f"skill://local/pdf-tools/{path}")
    return await r["gw"]._on_read_resource(ctx, params)


async def test_real_read_ok(real: dict[str, Any]) -> None:
    await real["gw"].apply_route("alice", _route("rr-1", [real["a"]]))
    res = await _read(real, "guide.md")
    assert res.contents[0].text == "# Guide"


@pytest.mark.parametrize(
    "path",
    [
        "../pdf-tools/guide.md",
        "..%2fpdf-tools%2fguide.md",
        "%2e%2e/guide.md",
        "/etc/passwd",
        "escape.md",  # symlink escape (in manifest, points outside)
        "unlisted.md",  # on disk, not in manifest
        "guide.md%00.png",
        "big.md",  # over resource_max_bytes
    ],
)
async def test_real_traversal_refused_curated(real: dict[str, Any], path: str) -> None:
    await real["gw"].apply_route("alice", _route("rr-1", [real["a"]]))
    with pytest.raises(MCPError) as ei:
        await _read(real, path)
    msg = ei.value.error.message
    assert ei.value.error.code == types.INVALID_PARAMS
    assert "TOP SECRET" not in msg and path not in msg and real["a"] not in msg


async def test_untyped_exception_is_curated_and_audited(
    real: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError(SECRET)

    monkeypatch.setattr("mcprouter.gateway.skills.read_body", boom)
    await real["gw"].apply_route("alice", _route("rr-1", [real["a"]]))
    ctx = _ctx(real["gw"], real["cat"], "alice")
    with pytest.raises(MCPError) as ei:
        await real["gw"]._on_get_prompt(ctx, types.GetPromptRequestParams(name="local/pdf-tools"))
    assert ei.value.error.code == types.INTERNAL_ERROR
    assert SECRET not in ei.value.error.message
    rows = [(r.outcome, r.detail) for r in _rows(real["db"])]
    assert ("error", "internal: RuntimeError") in rows
    assert not any(SECRET in (d or "") for _, d in rows)


async def test_untyped_exception_in_resolve_is_curated(
    real: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(real["gw"]._skills, "load_routed", boom)
    await real["gw"].apply_route("alice", _route("rr-1", [real["a"]]))
    with pytest.raises(MCPError) as ei:
        await _read(real, "guide.md")
    assert ei.value.error.code == types.INTERNAL_ERROR
    assert SECRET not in ei.value.error.message


async def test_clear_also_hides_skills(real: dict[str, Any]) -> None:
    gw = real["gw"]
    await gw.apply_route("alice", _route("rr-1", [real["a"]]))
    assert gw._routed_skills("alice")[0] == (real["a"],)
    gw.exposure.clear("alice")
    assert gw._routed_skills("alice") == ((), None)
    assert "alice" not in gw._skill_ids
    with pytest.raises(MCPError):
        await _read(real, "guide.md")


def test_slash_in_source_name_is_refused(real: dict[str, Any]) -> None:
    db = real["db"]
    with db() as s:
        src = s.get(SkillSourceRecord, s.get(SkillRecord, real["a"]).source_id)
        assert src is not None
        src.name = "a/b"
        s.commit()
    exp = real["gw"]._skills
    for name in ("a/b/pdf-tools", "a/b"):
        with pytest.raises(SkillAccessError) as ei:
            exp.resolve(name, [real["a"]])
        assert ei.value.message in ("Invalid skill name.", "Unknown skill.")
    # the id still resolves (unambiguous)
    assert exp.resolve(real["a"], [real["a"]])[0].id == real["a"]
