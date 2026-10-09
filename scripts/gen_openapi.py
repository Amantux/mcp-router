"""Regenerate docs/reference/openapi.json from the live app (wave-6 P-211).

    MCPR_DATABASE_URL=postgresql+psycopg://... .venv/bin/python scripts/gen_openapi.py

`create_app()` needs a reachable Postgres (it runs init_db). The output is
deterministic: `info.version` is pinned to "generated" so a version bump does
not churn the file (the real version lives in pyproject.toml). Pass `--check`
to exit 1 when the committed file is stale instead of writing it.
`tests/test_zz_route_coverage.py` fails when the committed file drifts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "reference" / "openapi.json"


def build_spec(database_url: str | None = None) -> dict[str, Any]:
    from mcprouter.api.app import create_app
    from mcprouter.settings import Settings

    # Default settings except the DB, so other MCPR_* env cannot change the
    # schema. Admin token set: no dev-mode warning (the schema ignores auth).
    url = database_url or Settings.from_env().database_url
    app = create_app(Settings(database_url=url), env={"MCPR_ADMIN_TOKEN": "x" * 40})
    spec: dict[str, Any] = app.openapi()
    spec["info"]["version"] = "generated"
    return spec


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render(build_spec())
    if "--check" in argv:
        current = OUT.read_text() if OUT.is_file() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale; run scripts/gen_openapi.py", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
