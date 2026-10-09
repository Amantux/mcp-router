"""Shared helpers moved out of tests/test_skills_exposure_activation.py (W0-2); not a test module."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.gateway.skills import SkillExposure
from mcprouter.models import SkillRecord, SkillSourceRecord
from tests.support.execution import FakeInvoker


class FakePolicy:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls = 0

    def check(
        self, agent_id: str, skill: SkillRecord, source: SkillSourceRecord
    ) -> tuple[bool, str]:
        self.calls += 1
        return self.allow, "rule r1"

    def check_many(
        self, agent_id: str, items: Sequence[tuple[SkillRecord, SkillSourceRecord]]
    ) -> list[tuple[bool, str]]:
        return [self.check(agent_id, sk, src) for sk, src in items]


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


def _clone(
    factory: sessionmaker[Session], skill_id: str, name: str, new_source: bool = False
) -> str:
    """Copy a skill under `name`; new_source=True puts it in a second source
    (names are unique per source, so cross-source duplicates are the real case)."""
    with factory() as s:
        sk = s.get(SkillRecord, skill_id)
        assert sk is not None
        source_id = sk.source_id
        if new_source:
            src = SkillSourceRecord(name="other", kind="directory", location="/nonexistent")
            s.add(src)
            s.flush()
            source_id = src.id
        c = SkillRecord(
            source_id=source_id,
            name=name,
            description="d",
            body="b",
            relative_path=name,
            resource_manifest=[],
            content_hash="0" * 64,
            manifest_hash="0" * 64,
        )
        s.add(c)
        s.commit()
        return c.id
