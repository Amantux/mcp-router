"""P-212: docs/reference/api.md is generated (scripts/gen_api_docs.py) and
must match the live app: a route, summary, docstring or auth class change
without regenerating is red."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from mcprouter.settings import Settings

from .conftest import requires_db

ROOT = Path(__file__).resolve().parents[1]


@requires_db
def test_api_md_is_current(settings: Settings) -> None:
    spec = importlib.util.spec_from_file_location(
        "gen_api_docs", ROOT / "scripts" / "gen_api_docs.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    live = mod.build(settings.database_url)
    committed = (ROOT / "docs" / "reference" / "api.md").read_text()
    assert "UNCLASSIFIED" not in live
    assert live == committed, "docs/reference/api.md is stale; run scripts/gen_api_docs.py"
