#!/usr/bin/env bash
# End-to-end smoke of a running compose stack. Usage:
#   MCPR_ADMIN_TOKEN=... MCPR_AGENT_KEY=... [BASE=http://127.0.0.1:<MCPR_HOST_PORT>] \
#   [COMPOSE="docker compose -p mcprsmoke"] scripts/smoke.sh
# (`make smoke` sets all of it up.) Exits non-zero on the first failed check.
# The LAST check stops the api container (graceful-stop timing); re-`up` to reuse.
set -euo pipefail
from_env_file() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1; }
# Same port compose published: env, else .env, else compose's default 8400.
port="${MCPR_HOST_PORT:-$(from_env_file MCPR_HOST_PORT)}"
BASE="${BASE:-http://127.0.0.1:${port:-8400}}"
COMPOSE="${COMPOSE:-docker compose -p mcprsmoke}"
: "${MCPR_ADMIN_TOKEN:?set MCPR_ADMIN_TOKEN}"
: "${MCPR_AGENT_KEY:?set MCPR_AGENT_KEY (the key part of MCPR_AGENT_KEYS=id:key)}"
# Compose requires POSTGRES_PASSWORD (D3) for every command it interpolates,
# including the restart/exec below: take it from .env, else mint one.
if [ -z "${POSTGRES_PASSWORD:-}" ]; then
  POSTGRES_PASSWORD=$(from_env_file POSTGRES_PASSWORD)
  [ -n "$POSTGRES_PASSWORD" ] || POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
fi
export POSTGRES_PASSWORD
ADM=(-H "Authorization: Bearer ${MCPR_ADMIN_TOKEN}")
AGT=(-H "Authorization: Bearer ${MCPR_AGENT_KEY}")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ok() { echo "PASS $*"; }
fail() { echo "FAIL $*" >&2; exit 1; }
code_of() { curl -s -o "$TMP/body" -w '%{http_code}' "$@"; }
start=$(date +%s)

wait_healthy() {  # bounded wait on the Docker HEALTHCHECK
  local cid st=""
  cid=$($COMPOSE ps -q api)
  [ -n "$cid" ] || fail "no api container"
  for _ in $(seq 1 60); do
    st=$(docker inspect -f '{{.State.Health.Status}}' "$cid")
    [ "$st" = healthy ] && return 0
    sleep 2
  done
  fail "api health=$st"
}

# 1. container healthy
wait_healthy; ok "HEALTHCHECK healthy"

# 2. dashboard at /
html=$(curl -fsS "$BASE/") || fail "GET /"
grep -q '<div id="root">' <<<"$html" || fail "GET / lacks app root"; ok "GET / 200 HTML with app root"

# 3. /healthz
curl -fsS "$BASE/healthz" | grep -q '"ok"' || fail "/healthz"; ok "GET /healthz $(curl -fsS "$BASE/healthz")"

# 4. auth is enforced: admin API, /mcp and /metrics (D14) reject a missing key
c=$(code_of "$BASE/api/v1/principals"); [ "$c" = 401 ] || fail "admin API without key -> $c (want 401)"
ok "GET /api/v1/principals without key -> 401"
c=$(code_of -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' "$BASE/mcp")
[ "$c" = 401 ] || fail "/mcp without key -> $c (want 401)"; ok "POST /mcp without key -> 401"
c=$(code_of -L "$BASE/metrics"); [ "$c" = 401 ] || fail "/metrics without key -> $c (want 401)"
c=$(code_of -L "${ADM[@]}" "$BASE/metrics"); [ "$c" = 200 ] || fail "/metrics with admin -> $c (want 200)"
ok "/metrics 401 without key, 200 with admin"

# 5. models health (admin)
mh=$(curl -fsS "${ADM[@]}" "$BASE/api/v1/models/health") || fail "models/health"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); k=d["decisionBackend"]["kind"]; assert k=="deterministic",k; print("PASS models/health decisionBackend.kind="+k)' "$mh"

# 6. setup status: MCPR_AGENT_KEYS seeds a principal at startup, so a keyed
# stack is already "set up" (needsSetup true only on a keyless first run).
ss=$(curl -fsS "${ADM[@]}" "$BASE/api/v1/setup/status") || fail "setup/status"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); n=d["counts"]["principals"]; assert n>=1 and d["needsSetup"] is False,d; print("PASS setup/status needsSetup=false principals=%d (env-seeded)" % n)' "$ss"

# 7. create a principal via admin -> key returned
pc=$(curl -fsS "${ADM[@]}" -H 'Content-Type: application/json' \
  -d '{"agentId":"smoke-created"}' "$BASE/api/v1/principals") || fail "POST principals"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d.get("apiKey"),d; print("PASS POST /principals apiKey returned (len %d)" % len(d["apiKey"]))' "$pc"

# 8. MCP handshake with the agent key (SDK client inside the api container)
$COMPOSE exec -T -e K="$MCPR_AGENT_KEY" api python - <<'PY' || fail "MCP handshake"
import asyncio, os
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
async def main():
    h = {"Authorization": "Bearer " + os.environ["K"]}
    # Installed SDK: headers ride on the http client (no headers= kwarg).
    async with create_mcp_http_client(headers=h) as hc, \
            streamable_http_client("http://127.0.0.1:8400/mcp", http_client=hc) as streams:
        r, w = streams[0], streams[1]
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            names = sorted(t.name for t in (await s.list_tools()).tools)
            assert any(n.startswith("router.") for n in names), names
            print("PASS MCP initialize server=%s tools=%s" % (init.server_info.name, names))
asyncio.run(main())
PY

# 9. route as the agent (no_match acceptable on an empty catalog)
c=$(code_of "${AGT[@]}" -H 'Content-Type: application/json' \
  -d '{"query":"list my files","maxTools":3}' "$BASE/api/v1/route")
[ "$c" = 200 ] || fail "POST /route -> $c $(cat "$TMP/body")"
ok "POST /api/v1/route 200 $(head -c 160 "$TMP/body")"

# 10. non-root
uid=$($COMPOSE exec -T api id -u | tr -d '\r')
[ "$uid" != 0 ] || fail "api runs as root"; ok "api uid=$uid (non-root)"

# 11. the image runs the checked-out version (D6)
want=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)
got=$($COMPOSE exec -T api python -c 'import mcprouter; print(mcprouter.__version__)' | tr -d '\r')
[ "$got" = "$want" ] || fail "container __version__=$got, checkout pyproject=$want"
ok "container __version__=$got == pyproject"

# 12. npx is on the image (D16: imported npx stdio servers must start)
nv=$($COMPOSE exec -T api npx --version | tr -d '\r') || fail "npx missing in the image"
ok "npx --version $nv"

# 13. state survives a restart (principal created in 7 is still listed)
$COMPOSE restart api >/dev/null
wait_healthy
pl=$(curl -fsS "${ADM[@]}" "$BASE/api/v1/principals") || fail "GET principals after restart"
python3 -c 'import json,sys; ids={p["agentId"] for p in json.loads(sys.argv[1])}; assert "smoke-created" in ids, ids; print("PASS principal survives compose restart")' "$pl"

# 14. graceful stop: SIGTERM -> clean exit 0 well inside the 30 s grace period
cid=$($COMPOSE ps -q api)
t0=$(date +%s)
docker stop -t 30 "$cid" >/dev/null
took=$(( $(date +%s) - t0 ))
rc=$(docker inspect -f '{{.State.ExitCode}}' "$cid")
[ "$rc" = 0 ] || fail "api exit code after docker stop = $rc (want 0)"
[ "$took" -lt 25 ] || fail "docker stop took ${took}s (want < 25 s: SIGTERM ignored?)"
ok "docker stop -t 30 -> exit 0 in ${took}s"

echo "SMOKE OK in $(( $(date +%s) - start ))s"
