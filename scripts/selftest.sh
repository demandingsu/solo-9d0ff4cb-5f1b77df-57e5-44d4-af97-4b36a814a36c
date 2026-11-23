#!/usr/bin/env bash
# End-to-end self-test against a running Docker Compose deployment.
#
# Usage:
#   docker compose up -d --build
#   ./scripts/selftest.sh                     # defaults to http://localhost:8000
#   BASE_URL=http://localhost:8000 ./scripts/selftest.sh
#
# Requires: curl, python3 (for JSON assertions only).

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
PYTHON="${PYTHON:-python3}"

say() { printf '\n== %s ==\n' "$*"; }

json_get() {
  "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"
}

wait_for_api() {
  for _ in $(seq 1 60); do
    if curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "API did not become healthy at $BASE_URL" >&2
  return 1
}

say "waiting for $BASE_URL"
wait_for_api
say "OpenAPI docs"
curl -fsS -o /dev/null -w 'GET /docs -> %{http_code}\n' "$BASE_URL/docs"
curl -fsS -o /dev/null -w 'GET /openapi.json -> %{http_code}\n' "$BASE_URL/openapi.json"

MANIFEST_VERSION=$(curl -fsS "$BASE_URL/versions/latest" | json_get 'd["version"]')
# Unique ids let the script be run repeatedly against the same volume.
RUN_ID="ST$(date +%Y%m%d%H%M%S)$$"
# Put this run's three finish times strictly before anything already stored,
# so the run always produces first/refresh/tie on its own.
FINISH_TIMES=$(curl -fsS "$BASE_URL/results?status=all" | "$PYTHON" -c '
import json, sys
from datetime import datetime, timedelta, timezone
rows = json.load(sys.stdin)
times = []
for row in rows:
    text = row["finish_time"].replace("Z", "+00:00")
    times.append(datetime.fromisoformat(text))
base = min(times) if times else datetime(2000, 1, 1, tzinfo=timezone.utc)
chosen = [base - timedelta(days=3), base - timedelta(days=2), base - timedelta(days=1)]
print(" ".join(t.isoformat().replace("+00:00", "Z") for t in chosen))
')
read -r T1 T2 T3 <<< "$FINISH_TIMES"
say "current latest version: $MANIFEST_VERSION (run id $RUN_ID)"
say "this run's finish times: $T1 / $T2 / $T3"

say "publish announcement (supplement + tie)"
V_PUBLISH=$(curl -fsS -X POST "$BASE_URL/announcements" \
  -H 'Content-Type: application/json' \
  -d "{
    \"base_version\": $MANIFEST_VERSION,
    \"supplements\": [
      {\"result_id\": \"${RUN_ID}-001\", \"athlete\": \"Selftest A\", \"finish_time\": \"$T1\", \"duration_ms\": 250000},
      {\"result_id\": \"${RUN_ID}-002\", \"athlete\": \"Selftest B\", \"finish_time\": \"$T2\", \"duration_ms\": 240000},
      {\"result_id\": \"${RUN_ID}-003\", \"athlete\": \"Selftest C\", \"finish_time\": \"$T3\", \"duration_ms\": 240000}
    ]
  }")
echo "$V_PUBLISH" | "$PYTHON" -m json.tool | head -60
PUBLISHED_VERSION=$(echo "$V_PUBLISH" | json_get 'd["version"]')
echo "$V_PUBLISH" | "$PYTHON" -c '
import json,sys
d=json.load(sys.stdin)
print("progression (first 6):", [(e["seq"], e["kind"], e["result_id"]) for e in d["progression"][:6]])
run_id = sys.argv[1]
run_entries = [e for e in d["progression"] if e["result_id"].startswith(run_id)]
assert [e["kind"] for e in run_entries] == ["first","refresh","tie"], run_entries
' "$RUN_ID"

say "stale base version is rejected"
STALE_CODE=$(curl -sS -o /tmp/selftest-stale.json -w '%{http_code}' -X POST "$BASE_URL/announcements" \
  -H 'Content-Type: application/json' \
  -d "{\"base_version\": $MANIFEST_VERSION, \"withdrawals\": [{\"result_id\": \"${RUN_ID}-002\"}]}")
echo "HTTP $STALE_CODE"; cat /tmp/selftest-stale.json; echo
[ "$STALE_CODE" = "409" ] || { echo "expected 409 for stale base version" >&2; exit 1; }

say "withdraw ${RUN_ID}-002 (only the result id is needed)"
V_WITHDRAW=$(curl -fsS -X POST "$BASE_URL/announcements" \
  -H 'Content-Type: application/json' \
  -d "{\"base_version\": $PUBLISHED_VERSION, \"withdrawals\": [{\"result_id\": \"${RUN_ID}-002\"}]}")
WITHDRAWN_VERSION=$(echo "$V_WITHDRAW" | json_get 'd["version"]')
echo "$V_WITHDRAW" | "$PYTHON" -c '
import json,sys
d=json.load(sys.stdin)
print("version:", d["version"])
print("recompute_from:", d["recompute_from_finish_time"],
      "reused_prefix:", d["reused_prefix_entries"],
      "recomputed:", d["recomputed_active_results"])
run_id = sys.argv[1]
print("progression (first 6):", [(e["seq"], e["kind"], e["result_id"]) for e in d["progression"][:6]])
assert not any(e["result_id"] == run_id + "-002" for e in d["progression"])
assert d["parent_version"] == int(sys.argv[2])
' "$RUN_ID" "$PUBLISHED_VERSION"

say "look back at the older version"
curl -fsS "$BASE_URL/versions/$PUBLISHED_VERSION/progression" | "$PYTHON" -c '
import json,sys
entries=json.load(sys.stdin)
run_id = sys.argv[1]
print("v-old progression (first 6):", [(e["seq"], e["kind"], e["result_id"]) for e in entries[:6]])
assert any(e["result_id"] == run_id + "-002" for e in entries)
' "$RUN_ID"
curl -fsS "$BASE_URL/versions/$PUBLISHED_VERSION" | "$PYTHON" -c '
import json,sys
d=json.load(sys.stdin)
print("v-old active results:", [r["result_id"] for r in d["active_results"]])
'

say "all published versions"
curl -fsS "$BASE_URL/versions" | "$PYTHON" -c '
import json,sys
versions=json.load(sys.stdin)
print([(v["version"], v["parent_version"]) for v in versions])
'

say "result registry"
curl -fsS "$BASE_URL/results?status=all" | "$PYTHON" -m json.tool

say "self-test passed"
echo "published version $PUBLISHED_VERSION, withdrawal version $WITHDRAWN_VERSION"
