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


# --- P-103 (D3): env reach + container hardening ----------------------------

# Compose knobs that are not MCPR_* (db container / torch runtime); the MCPR_*
# compose-only knobs are declared in settings.ENV_ONLY_SPEC.
NON_ROUTER_COMPOSE = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "OMP_NUM_THREADS",
}
# Non-MCPR container env the router image understands.
EXTERNAL_ENV = {"HF_HOME"}


def registry_vars() -> set[str]:
    """Every declared variable: SETTINGS_SPEC (+ `_FILE` variants) and the
    entrypoint/compose-only ENV_ONLY_SPEC."""
    from mcprouter.settings import ENV_ONLY_SPEC, SETTINGS_SPEC

    names = {s.env for s in (*SETTINGS_SPEC, *ENV_ONLY_SPEC)}
    return names | {s.file_var for s in SETTINGS_SPEC if s.file_var}


COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.dev.yml",
    "docker-compose.inference.yml",
    "docker-compose.gpu.yml",
)


def test_api_loads_env_file_optionally() -> None:
    assert _api()["env_file"] == [{"path": ".env", "required": False}]


def test_explicit_api_env_names_are_registered() -> None:
    reg = registry_vars()
    for name in COMPOSE_FILES:
        api = _load(name).get("services", {}).get("api", {})
        for var in api.get("environment", {}):
            assert var in reg or var in EXTERNAL_ENV, f"{name}: {var} is not a router setting"


def test_interpolated_vars_are_known() -> None:
    import re

    reg = registry_vars()
    for name in COMPOSE_FILES:
        text = "\n".join(
            ln for ln in (ROOT / name).read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        for var in re.findall(r"\$\{([A-Z0-9_]+)", text):
            assert var in reg or var in NON_ROUTER_COMPOSE, f"{name}: unknown ${{{var}}}"


def test_db_publishes_no_port_in_base() -> None:
    assert "ports" not in _load("docker-compose.yml")["services"]["db"]


def test_postgres_password_required_without_default() -> None:
    text = (ROOT / "docker-compose.yml").read_text()
    assert "${POSTGRES_PASSWORD:-" not in text
    assert text.count("${POSTGRES_PASSWORD:?") == 2  # db env + derived DSN


def test_api_container_hardening() -> None:
    api = _api()
    assert api["init"] is True
    assert api["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in api["security_opt"]
    assert api["pids_limit"] == 512
    assert api["stop_grace_period"] == "30s"


def test_overrides_only_extend_known_services() -> None:
    base = set(_load("docker-compose.yml")["services"])
    allowed_keys = {
        "build",
        "image",
        "environment",
        "healthcheck",
        "deploy",
        "ports",
    }
    for name in COMPOSE_FILES[1:]:
        services = _load(name)["services"]
        assert set(services) <= base, name
        for svc, body in services.items():
            assert set(body) <= allowed_keys, f"{name}:{svc} {set(body) - allowed_keys}"


def test_entrypoint_graceful_timeout_fits_stop_grace() -> None:
    import re

    ep = (ROOT / "scripts" / "docker-entrypoint.sh").read_text()
    m = re.search(r"--timeout-graceful-shutdown (\d+)", ep)
    assert m and int(m.group(1)) < 30
