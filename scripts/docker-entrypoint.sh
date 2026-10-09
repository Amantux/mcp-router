#!/bin/sh
# MCP Router container entrypoint: validate settings -> check writable dirs ->
# wait for Postgres (bounded) -> migrate to head -> exec uvicorn with ONE worker.
# Single worker is deliberate: the lifespan owns the sync/rollup loops and the
# in-process model load; N workers would run N loops and load N models.
#
# Exit codes: 2 = invalid setting (the FATAL line names the variable),
#             1 = anything else (unwritable dir, database unreachable).
#
# Bind posture (D1, fail closed): the shell never parses config itself; it asks
# mcprouter.settings for the resolved plan. MCPR_ADMIN_TOKEN set -> 0.0.0.0.
# Unset -> 127.0.0.1 (reachable only from inside the container) with a
# WARNING, unless MCPR_ALLOW_OPEN_DEV=1 opts in to all interfaces.
set -eu

log() { echo "[entrypoint] $*" >&2; }
die() { log "FATAL: $*"; exit 1; }

[ -n "${MCPR_DATABASE_URL:-}${MCPR_DATABASE_URL_FILE:-}" ] \
  || die "MCPR_DATABASE_URL (or MCPR_DATABASE_URL_FILE) is required (see docs/deploy.md)."

# "<bind_host> <db_wait_tries> <port>" on stdout; FATAL/WARNING on stderr.
plan=$(python -m mcprouter.settings entrypoint) || exit $?
bind_host=${plan%% *}
rest=${plan#* }
tries=${rest%% *}
port=${rest#* }

# MCPR_DATA_DIR: entrypoint-only knob (tests point it at a temp dir).
for d in "${MCPR_DATA_DIR:-/data}" "${MCPR_SKILLS_CACHE_DIR:-/srv/skills-cache}" "${MCPR_MODELS_CACHE_DIR:-/srv/models-cache}"; do
  mkdir -p "$d" 2>/dev/null || true
  probe="$d/.write-probe.$$"
  if ! ( : > "$probe" ) 2>/dev/null; then
    die "$d is not writable by uid $(id -u). A bind mount or read-only volume overrode the image's ownership: chown it to $(id -u):$(id -g)."
  fi
  rm -f "$probe"
done

python - "$tries" <<'PY' || die "Postgres not reachable after $tries attempts (MCPR_DATABASE_URL host/credentials?)."
import sys, time
from sqlalchemy import create_engine, text
from mcprouter.settings import Settings
url = Settings.from_env().database_url
tries = int(sys.argv[1])
eng = create_engine(url, pool_pre_ping=True)
for i in range(1, tries + 1):
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        print(f"[entrypoint] database reachable (attempt {i})", file=sys.stderr)
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - only the exception TYPE is logged (DSN may carry a password)
        print(f"[entrypoint] waiting for database ({i}/{tries}): {type(exc).__name__}", file=sys.stderr)
        if i < tries:
            time.sleep(2)
sys.exit(1)
PY

# A pass-through command (e.g. `python -m mcprouter.migrate downgrade -1`) runs
# against the schema as it is; only the server path upgrades first.
if [ "$#" -gt 0 ]; then exec "$@"; fi

# Schema to head before serving (E6 P-601): the CLI takes the same advisory
# lock as the app's boot path, prints FATAL itself (no DSN) and exits 1.
rev=$(python -m mcprouter.migrate upgrade) || die "schema migration failed (see the FATAL line above)."
log "database schema at revision $rev"

exec uvicorn --factory mcprouter.api.app:create_app \
  --host "$bind_host" --port "$port" --workers 1 \
  --timeout-graceful-shutdown 20
