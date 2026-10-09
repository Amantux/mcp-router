#!/bin/sh
# MCP Router container entrypoint: validate -> check writable dirs ->
# wait for Postgres (bounded) -> exec uvicorn with ONE worker.
# Single worker is deliberate: the lifespan owns the sync/rollup loops and the
# in-process model load; N workers would run N loops and load N models.
set -eu

log() { echo "[entrypoint] $*" >&2; }
die() { log "FATAL: $*"; exit 1; }

[ -n "${MCPR_DATABASE_URL:-}" ] || die "MCPR_DATABASE_URL is required (see docs/deploy.md)."

if [ -z "${MCPR_ADMIN_TOKEN:-}" ]; then
  log "WARNING: MCPR_ADMIN_TOKEN is unset. With no agent keys the management API"
  log "WARNING: is OPEN (dev mode); with agent keys it is locked (403). Set it for"
  log "WARNING: any deployment reachable by anyone but you."
fi

for d in /data "${MCPR_SKILLS_CACHE_DIR:-/srv/skills-cache}" "${MCPR_MODELS_CACHE_DIR:-/srv/models-cache}"; do
  mkdir -p "$d" 2>/dev/null || true
  probe="$d/.write-probe.$$"
  if ! ( : > "$probe" ) 2>/dev/null; then
    die "$d is not writable by uid $(id -u). A bind mount or read-only volume overrode the image's ownership: chown it to $(id -u):$(id -g)."
  fi
  rm -f "$probe"
done

tries="${MCPR_DB_WAIT_TRIES:-30}"
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
        time.sleep(2)
sys.exit(1)
PY

if [ "$#" -gt 0 ]; then exec "$@"; fi
exec uvicorn --factory mcprouter.api.app:create_app \
  --host 0.0.0.0 --port "${MCPR_PORT:-8400}" --workers 1
