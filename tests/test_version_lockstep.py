"""MT-7 version lockstep (D6, P-509): one version, everywhere it is shown."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import mcprouter
from mcprouter.api.app import create_app
from mcprouter.settings import Settings
from tests.conftest import TEST_DB_URL

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> str:
    version: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    return version


def test_package_version_is_pyproject_version() -> None:
    assert mcprouter.__version__ == _pyproject()


def test_rest_and_mcp_report_the_package_version() -> None:
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    try:
        assert app.version == _pyproject()
        init = app.state.gateway.server.create_initialization_options()
        assert init.server_version == _pyproject()
    finally:
        app.state.engine.dispose()


def test_ui_package_version_matches() -> None:
    pkg = json.loads((ROOT / "ui" / "package.json").read_text())
    assert pkg["version"] == _pyproject()


def test_top_changelog_heading_is_the_version() -> None:
    lines = (ROOT / "CHANGELOG.md").read_text().splitlines()
    heading = next(line[3:].strip() for line in lines if line.startswith("## "))
    assert heading == _pyproject()
