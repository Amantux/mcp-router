"""`eval_results` table — owned by the eval package, NOT models.py.

models.py is frozen for feature branches, so this table lives on its own
`MetaData` and is created idempotently (`CREATE TABLE IF NOT EXISTS`
semantics via checkfirst) the first time an evaluation is stored. At
integration it can move into models.py + an Alembic revision unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    insert,
)
from sqlalchemy.orm import Session

eval_metadata = MetaData()

eval_results = Table(
    "eval_results",
    eval_metadata,
    Column("id", String(36), primary_key=True),
    Column("dataset", String(120), nullable=False, index=True),
    Column("model_version", String(80), nullable=False),
    Column("case_count", Integer, nullable=False),
    Column("metrics", JSON, nullable=False),
    Column("cases", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
)


def ensure_eval_table(engine: Engine) -> None:
    eval_metadata.create_all(engine, checkfirst=True)


def save_eval_result(
    s: Session,
    *,
    dataset: str,
    model_version: str,
    metrics: dict[str, Any],
    cases: list[dict[str, Any]],
) -> str:
    rid = str(uuid.uuid4())
    s.execute(
        insert(eval_results).values(
            id=rid,
            dataset=dataset,
            model_version=model_version[:80],
            case_count=len(cases),
            metrics=metrics,
            cases=cases,
            created_at=datetime.now(UTC),
        )
    )
    return rid
