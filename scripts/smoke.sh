#!/usr/bin/env bash
# End-to-end smoke of a running compose stack. Usage:
#   MCPR_ADMIN_TOKEN=... MCPR_AGENT_KEY=... [BASE=http://127.0.0.1:8450] \
#   [COMPOSE="docker compose -p mcprsmoke"] scripts/smoke.sh
# Exits non-zero on the first failed check.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8450}"
COMPOSE="${COMPOSE:-docker compose -p mcprsmoke}"
: "${MCPR_ADMIN_TOKEN:?set MCPR_ADMIN_TOKEN}"
: "${MCPR_AGENT_KEY:?set MCPR_AGENT_KEY (the key part of MCPR_AGENT_KEYS=id:key)}"
ADM=(-H "Authorization: Bearer ${MCPR_ADMIN_TOKEN}")
AGT=(-H "Authorization: Bearer ${MCPR_AGENT_KEY}")
ok() { echo "PASS $*"; }
fail() { echo "FAIL $*" >&2; exit 1; }
start=$(date +%s)

# 1. container healthy (Docker HEALTHCHECK), bounded wait
cid=$($COMPOSE ps -q api)
[ -n "$cid" ] || fail "no api container"
for _ in $(seq 1 60); do
  st=$(docker inspect -f '{{.State.Health.Status}}' "$cid")
  [ "$st" = healthy ] && break; sleep 2
done
[ "$st" = healthy ] || fail "api health=$st"; ok "HEALTHCHECK healthy"

# 2. dashboard at /
html=$(curl -fsS "$BASE/") || fail "GET /"
grep -q '<div id="root">' <<<"$html" || fail "GET / lacks app root"; ok "GET / 200 HTML with app root"

# 3. /healthz
curl -fsS "$BASE/healthz" | grep -q '"ok"' || fail "/healthz"; ok "GET /healthz $(curl -fsS "$BASE/healthz")"

# 4. models health (admin)
mh=$(curl -fsS "${ADM[@]}" "$BASE/api/v1/models/health") || fail "models/health"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); k=d["decisionBackend"]["kind"]; assert k=="deterministic",k; print("PASS models/health decisionBackend.kind="+k)' "$mh"

# 5. setup status before any principal exists
ss=$(curl -fsS "${ADM[@]}" "$BASE/api/v1/setup/status") || fail "setup/status"
# MCPR_AGENT_KEYS seeds a principal at startup, so a keyed stack is already
# "set up": needsSetup must be false with principals >= 1 (true only on a
# keyless first run).
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); n=d["counts"]["principals"]; assert n>=1 and d["needsSetup"] is False,d; print("PASS setup/status needsSetup=false principals=%d (env-seeded)" % n)' "$ss"

# 6. create a principal via admin -> key returned
pc=$(curl -fsS "${ADM[@]}" -H 'Content-Type: application/json' \
  -d '{"agentId":"smoke-created"}' "$BASE/api/v1/principals") || fail "POST principals"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d.get("apiKey"),d; print("PASS POST /principals apiKey returned (len %d)" % len(d["apiKey"]))' "$pc"

# 7. MCP handshake with the agent key (SDK client inside the api container)
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

# 8. route as the agent (no_match acceptable on an empty catalog)
code=$(curl -s -o /tmp/mcpr-smoke-route.json -w '%{http_code}' "${AGT[@]}" \
  -H 'Content-Type: application/json' -d '{"query":"list my files","maxTools":3}' "$BASE/api/v1/route")
[ "$code" = 200 ] || fail "POST /route -> $code $(cat /tmp/mcpr-smoke-route.json)"
ok "POST /api/v1/route 200 $(head -c 160 /tmp/mcpr-smoke-route.json)"

# 9. non-root
uid=$($COMPOSE exec -T api id -u | tr -d '\r')
[ "$uid" != 0 ] || fail "api runs as root"; ok "api uid=$uid (non-root)"

echo "SMOKE OK in $(( $(date +%s) - start ))s"
