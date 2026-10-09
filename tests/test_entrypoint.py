"""MT-5 (entrypoint part): run the REAL scripts/docker-entrypoint.sh under
`sh` with stub `uvicorn` (prints its argv) and a `python` shim on PATH.

The shim delegates to the real interpreter (so the settings adapter is the
production code), except the DB-wait heredoc (`python -`), which it answers
with STUB_DB_RC when set. No docker, no database."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
TOKEN = "t" * 40  # >= 32 chars (P-106 minimum)
DSN = "postgresql+psycopg://mcprouter:canary-db-pw@127.0.0.1:1/mcprouter"


@pytest.fixture()
def stub_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python"
    py.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-" ] && [ -n "${STUB_DB_RC:-}" ]; then\n'
        '  cat >/dev/null; exit "$STUB_DB_RC"\n'
        "fi\n"
        'exec "$REAL_PYTHON" "$@"\n'
    )
    uv = bindir / "uvicorn"
    uv.write_text('#!/bin/sh\necho "UVICORN_ARGV: $*"\n')
    for f in (py, uv):
        f.chmod(0o755)
    return bindir


def run(
    stub_bin: Path, tmp_path: Path, env: dict[str, str | None], *args: str
) -> subprocess.CompletedProcess[str]:
    base: dict[str, str | None] = {
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "REAL_PYTHON": sys.executable,
        "PYTHONPATH": os.environ.get("PYTHONPATH"),
        "MCPR_DATA_DIR": str(tmp_path / "data"),
        "MCPR_SKILLS_CACHE_DIR": str(tmp_path / "skills"),
        "MCPR_MODELS_CACHE_DIR": str(tmp_path / "models"),
        "MCPR_DATABASE_URL": DSN,
        "STUB_DB_RC": "0",
    }
    base.update(env)
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *args],
        env={k: v for k, v in base.items() if v is not None},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _argv(out: str) -> list[str]:
    lines = [ln for ln in out.splitlines() if ln.startswith("UVICORN_ARGV: ")]
    assert len(lines) == 1, out
    return lines[0].removeprefix("UVICORN_ARGV: ").split()


def _host(out: str) -> str:
    argv = _argv(out)
    return argv[argv.index("--host") + 1]


# --- P-101 (D1): bind posture ------------------------------------------------


def test_no_admin_token_binds_loopback_and_warns(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {})
    assert r.returncode == 0, r.stderr
    assert _host(r.stdout) == "127.0.0.1"
    assert "WARNING: admin API open; listening on loopback only; set MCPR_ADMIN_TOKEN" in r.stderr


def test_admin_token_binds_all_interfaces(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {"MCPR_ADMIN_TOKEN": TOKEN, "MCPR_ALLOWED_HOSTS": "router.lan"})
    assert r.returncode == 0, r.stderr
    assert _host(r.stdout) == "0.0.0.0"
    assert "WARNING" not in r.stderr
    assert TOKEN not in r.stdout + r.stderr


def test_admin_token_file_binds_all_interfaces(stub_bin: Path, tmp_path: Path) -> None:
    f = tmp_path / "admin"
    f.write_text(TOKEN + "\n")
    r = run(stub_bin, tmp_path, {"MCPR_ADMIN_TOKEN_FILE": str(f)})
    assert r.returncode == 0, r.stderr
    assert _host(r.stdout) == "0.0.0.0"
    assert TOKEN not in r.stdout + r.stderr


def test_token_without_allowed_hosts_warns_about_421(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {"MCPR_ADMIN_TOKEN": TOKEN})
    assert _host(r.stdout) == "0.0.0.0"
    assert "MCPR_ALLOWED_HOSTS is empty" in r.stderr


def test_allow_open_dev_binds_all_interfaces_with_warning(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {"MCPR_ALLOW_OPEN_DEV": "1"})
    assert r.returncode == 0, r.stderr
    assert _host(r.stdout) == "0.0.0.0"
    assert "MCPR_ALLOW_OPEN_DEV=1 binds ALL interfaces" in r.stderr


def test_agent_keys_without_token_still_loopback(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {"MCPR_AGENT_KEYS": "a:" + "k" * 40})
    assert r.returncode == 0, r.stderr
    assert _host(r.stdout) == "127.0.0.1"
    assert "admin API locked (agent keys set); listening on loopback only" in r.stderr


def test_single_worker_and_port(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {"MCPR_ADMIN_TOKEN": TOKEN, "MCPR_PORT": " 8871 "})
    argv = _argv(r.stdout)
    assert argv[argv.index("--workers") + 1] == "1"
    assert argv[argv.index("--port") + 1] == "8871"


# --- P-111: guards before the pass-through ----------------------------------


def test_missing_database_url_is_fatal(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {"MCPR_DATABASE_URL": ""})
    assert r.returncode == 1
    assert "FATAL: MCPR_DATABASE_URL (or MCPR_DATABASE_URL_FILE) is required" in r.stderr
    assert "UVICORN_ARGV" not in r.stdout


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("MCPR_ALLOW_OPEN_DEV", "maybe"),
        ("MCPR_DB_WAIT_TRIES", "0"),
        ("MCPR_DB_WAIT_TRIES", "x"),
        ("MCPR_EMBED_BATCH_SIZE", "nan"),
        ("MCPR_EMBEDDING_BACKEND", "local"),
        ("MCPR_PORT", "70000"),
    ],
)
def test_invalid_setting_exits_2_naming_the_var(
    stub_bin: Path, tmp_path: Path, var: str, value: str
) -> None:
    r = run(stub_bin, tmp_path, {var: value})
    assert r.returncode == 2, r.stderr
    assert f"FATAL: {var}" in r.stderr
    assert "UVICORN_ARGV" not in r.stdout


def test_unwritable_dir_is_fatal(stub_bin: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    r = run(stub_bin, tmp_path, {"MCPR_DATA_DIR": str(blocker / "sub")})
    assert r.returncode == 1
    assert "is not writable" in r.stderr


def test_unreachable_db_exits_1_without_dsn_password(stub_bin: Path, tmp_path: Path) -> None:
    # Real DB-wait code (no STUB_DB_RC): port 1 refuses; one attempt.
    r = run(stub_bin, tmp_path, {"STUB_DB_RC": None, "MCPR_DB_WAIT_TRIES": "1"})
    assert r.returncode == 1, r.stderr
    assert "Postgres not reachable after 1 attempts" in r.stderr
    assert "waiting for database (1/1): OperationalError" in r.stderr
    assert "canary-db-pw" not in r.stdout + r.stderr
    assert "UVICORN_ARGV" not in r.stdout


def test_pass_through_command_runs_after_guards(stub_bin: Path, tmp_path: Path) -> None:
    r = run(stub_bin, tmp_path, {"MCPR_ADMIN_TOKEN": TOKEN}, "echo", "passthrough-ok")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "passthrough-ok"
    # ...and the guards still ran first: a bad setting stops the pass-through.
    bad = run(stub_bin, tmp_path, {"MCPR_DB_WAIT_TRIES": "0"}, "echo", "passthrough-ok")
    assert bad.returncode == 2
    assert "passthrough-ok" not in bad.stdout
