"""Compose/registry names shared by the MT-5 contract tests
(test_compose_contract.py, test_config_registry.py)."""

from __future__ import annotations

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
