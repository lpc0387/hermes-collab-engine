#!/usr/bin/env bash
# opencode-proxy-api integration test suite
# Usage: ./test/run.sh [server_url] [model]
#   server_url  default http://localhost:8080
#   model       default deepseek-v4-flash-free
#
# Tests are reproducible — pass/fail determined by JSON response shape.
# A running proxy server must already be started before calling this script.

set -euo pipefail

BASE="${1:-http://localhost:8080}"
MODEL="${2:-deepseek-v4-flash-free}"
PASS=0
FAIL=0
INFO=""

ok()    { PASS=$((PASS+1)); echo "  PASS"; }
info()  { INFO="${INFO}  $1\n"; echo "  INFO: $1"; }
fail()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

# ──────────────────────────────────────────────────────────────────────────────
#  1.  /health
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 1. GET /health ==="
resp=$(curl -s "$BASE/health")
if echo "$resp" | grep -q '"ok"'; then ok; else fail "health: $resp"; fi

# ──────────────────────────────────────────────────────────────────────────────
#  2.  /v1/chat/completions  non-stream
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 2. POST /v1/chat/completions non-stream ==="
resp=$(curl -s --max-time 90 "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"say hi"}],"stream":false}')
if echo "$resp" | grep -q '"finish_reason":"stop"'; then ok; else fail "$(echo $resp | head -c200)"; fi

# ──────────────────────────────────────────────────────────────────────────────
#  3.  /v1/chat/completions  stream
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 3. POST /v1/chat/completions stream ==="
resp=$(curl -s --max-time 90 "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"say hi"}],"stream":true}')
if echo "$resp" | grep -q 'data:.*\[DONE\]'; then ok; else fail "$(echo $resp | head -c200)"; fi

# ──────────────────────────────────────────────────────────────────────────────
#  4.  /v1/responses  non-stream
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 4. POST /v1/responses non-stream ==="
resp=$(curl -s --max-time 90 "$BASE/v1/responses" \
  -H "Content-Type: application/json" \
  -d '{"model":"'"$MODEL"'","input":[{"role":"user","content":[{"type":"input_text","text":"say hi"}]}],"stream":false}')
if echo "$resp" | grep -q '"type":"output_text"'; then ok; else fail "$(echo $resp | head -c200)"; fi
# Responses API returns "stop_reason":"end_turn" or "stop"
if echo "$resp" | grep -q '"stop_reason":"end_turn\|"stop_reason":"stop"'; then ok; else fail "responses non-stream stop_reason: $(echo $resp | head -c200)"; fi

# ──────────────────────────────────────────────────────────────────────────────
#  5.  /v1/responses  stream
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 5. POST /v1/responses stream ==="
resp=$(curl -s --max-time 90 "$BASE/v1/responses" \
  -H "Content-Type: application/json" \
  -d '{"model":"'"$MODEL"'","input":[{"role":"user","content":[{"type":"input_text","text":"say hi"}]}],"stream":true}')
if echo "$resp" | grep -q 'data:.*\[DONE\]'; then ok; else fail "$(echo $resp | head -c200)"; fi

# ──────────────────────────────────────────────────────────────────────────────
#  6.  /v1/messages (Claude format) — proxy test only
#     NOTE: deepseek-v4-flash-free does NOT support Claude format upstream.
#     This test verifies the proxy correctly routes the request (gets a
#     meaningful response from upstream, not a 404/proxy error).
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 6. POST /v1/messages (Claude format) ==="
resp=$(curl -s --max-time 30 "$BASE/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: public" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"say hi"}],"max_tokens":256}')
# The proxy forwarded it — we get a JSON error from upstream, not a 404/502
# from the proxy itself.
http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 "$BASE/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: public" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"say hi"}],"max_tokens":256}')
if [ "$http_code" = "200" ]; then
  info "endpoint returned 200 (may be upstream error if model unsupported)"
  ok
elif [ "$http_code" = "400" ]; then
  # 400 from upstream is expected for models that don't support Claude format
  info "upstream returned 400 (model doesn't support Claude format) — proxy routing OK"
  ok
else
  fail "unexpected HTTP $http_code (proxy may not be routing correctly)"
fi

# ──────────────────────────────────────────────────────────────────────────────
#  7.  Unknown endpoint → 404
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 7. Unknown endpoint returns 404 ==="
http_code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/v1/unknown")
if [ "$http_code" = "404" ]; then ok; else fail "expected 404, got $http_code"; fi

# ──────────────────────────────────────────────────────────────────────────────
#  8.  Method not allowed (GET on POST endpoint)
# ──────────────────────────────────────────────────────────────────────────────
echo "=== 8. GET on /v1/chat/completions returns 405 ==="
http_code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/v1/chat/completions")
if [ "$http_code" = "405" ]; then ok; else fail "expected 405, got $http_code"; fi

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────
echo "============================="
echo -e "Results: $PASS passed, $FAIL failed"
if [ -n "$INFO" ]; then echo -e "$INFO"; fi
echo "============================="
exit $FAIL
