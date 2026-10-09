"""Owner decision (wave 4): ids that may carry the "skill:<uuid>" prefix
(41 chars) must fit in duplicate_suggestions + tool_stats_daily."""

from __future__ import annotations

from mcprouter import models
from mcprouter.db import _ADDITIVE_COLUMNS

SKILL_REF_LEN = len("skill:") + 36


def test_prefixed_id_columns_are_48_wide() -> None:
    cols = [
        models.DuplicateSuggestion.__table__.c.tool_a_id,
        models.DuplicateSuggestion.__table__.c.tool_b_id,
        models.DuplicateSuggestion.__table__.c.preferred_tool_id,
        models.ToolStatsDaily.__table__.c.tool_id,
    ]
    for col in cols:
        assert col.type.length == 48, col.name
        assert col.type.length >= SKILL_REF_LEN


def test_widening_ddl_present_for_existing_databases() -> None:
    expected = {
        ("duplicate_suggestions", "tool_a_id"),
        ("duplicate_suggestions", "tool_b_id"),
        ("duplicate_suggestions", "preferred_tool_id"),
        ("tool_stats_daily", "tool_id"),
    }
    for table, col in expected:
        stmt = f"ALTER TABLE {table} ALTER COLUMN {col} TYPE VARCHAR(48)"
        assert stmt in _ADDITIVE_COLUMNS, stmt
