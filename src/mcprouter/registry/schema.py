"""Registry-owned indexes that models.py does not express. Since 0.6 they are
created by migration 0001 (frozen copy of this DDL); `init_registry` only
fills in a missing one — see docs/history/INTEGRATION_NOTES-registry.md.

* `ix_tools_fts` — GIN expression index for keyword search over
  name (weight A) + description (B) + tags (C). The query in catalog.py is
  built from the SAME template, and a test asserts the planner uses this
  index, so the two cannot silently drift.
* `ux_dup_pair` — unique (tool_a_id, tool_b_id) on duplicate_suggestions.
  Pairs are stored normalised (a < b), so this makes concurrent dedup runs
  unable to insert the same pair twice (INSERT ... ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

from sqlalchemy import Connection, Engine, text

# `{t}` is the column qualifier: "" for the index DDL, "mcp_tools." for
# queries that join other tables. Postgres matches the expression structurally,
# so both forms use the index.
#
# - camelCase names are split (`createIssue` -> `create Issue`) and `_ . - /`
#   become spaces, so name words are indexed as words, not as one host-like
#   token.
# - tags is a `json` column; `json::text` is an immutable cast, so it is legal
#   in an index expression. Brackets/quotes/commas are blanked out.
_TSV_TEMPLATE = (
    "(setweight(to_tsvector('english'::regconfig, "
    "translate(regexp_replace({t}name, '([a-z0-9])([A-Z])', '\\1 \\2', 'g'), '_.-/', '    ')), 'A')"
    " || setweight(to_tsvector('english'::regconfig, coalesce({t}description, '')), 'B')"
    " || setweight(to_tsvector('english'::regconfig, "
    "translate({t}tags::text, '[]\",_-', '       ')), 'C'))"
)


def tsv_sql(qualifier: str = "") -> str:
    return _TSV_TEMPLATE.format(t=qualifier)


def index_exists(conn: Connection, name: str) -> bool:
    """Catalog read (no DDL): does an index (or any relation) named `name` exist?"""
    return conn.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar() is not None


def init_registry(engine: Engine) -> None:
    """Both indexes ship in migration 0001; this only creates one that is
    missing (a database built without migrations), so a normal boot issues
    zero DDL."""
    with engine.begin() as conn:
        if not index_exists(conn, "ix_tools_fts"):
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_tools_fts ON mcp_tools USING GIN ({tsv_sql()})"
                )
            )
        if not index_exists(conn, "ux_dup_pair"):
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_dup_pair "
                    "ON duplicate_suggestions (tool_a_id, tool_b_id)"
                )
            )
