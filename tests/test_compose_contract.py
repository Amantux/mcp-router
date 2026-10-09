"""MT-5 (compose part): pure-YAML contract checks on docker-compose*.yml.
No docker required."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((ROOT / name).read_text())
    assert isinstance(data, dict)
    return data


def _api() -> dict[str, Any]:
    svc = _load("docker-compose.yml")["services"]["api"]
    assert isinstance(svc, dict)
    return svc


# --- P-101 (D1) ---------------------------------------------------------------


def test_api_port_binds_loopback_by_default() -> None:
    ports = _api()["ports"]
    assert ports == ["${MCPR_BIND:-127.0.0.1}:${MCPR_HOST_PORT:-8400}:8400"]
