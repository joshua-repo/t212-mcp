#!/usr/bin/env bash
# End-to-end health check for a deployed t212-mcp. Run on the VPS as root
# (it reads the service's .env and oauth_state.json):
#
#   sudo deploy/healthcheck.sh            # all checks, incl. one read-only T212 API call
#   sudo deploy/healthcheck.sh --no-t212  # skip the T212 API call
#
# Exits non-zero if any check fails. The authenticated checks reuse an
# unexpired access token from oauth_state.json (i.e. Claude must have
# connected at least once); they are skipped, not failed, if none exists.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/t212-mcp}"
SERVICE="${SERVICE:-t212-mcp}"
CALL_T212=1
[[ "${1:-}" == "--no-t212" ]] && CALL_T212=0

PY="$APP_DIR/.venv/bin/python"
failures=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; }

env_get() { grep -E "^$1=" "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"; }
PUBLIC_URL="$(env_get MCP_PUBLIC_URL)"; PUBLIC_URL="${PUBLIC_URL%/}"
HOST="$(env_get HOST)"; HOST="${HOST:-127.0.0.1}"
PORT="$(env_get PORT)"; PORT="${PORT:-8000}"
[[ -z "$PUBLIC_URL" ]] && { echo "MCP_PUBLIC_URL not set in $APP_DIR/.env"; exit 2; }

tmp="$(mktemp -d)"; chmod 700 "$tmp"; trap 'rm -rf "$tmp"' EXIT
http_code() { curl -s -m 10 -o /dev/null -w '%{http_code}' "$@"; }

echo "t212-mcp health check ($PUBLIC_URL)"

echo "Service"
if systemctl is-active --quiet "$SERVICE"; then pass "systemd unit $SERVICE is active"
else fail "systemd unit $SERVICE is not active"; fi

if ss -ltnH "sport = :$PORT" | grep -q "$HOST:$PORT"; then pass "listening on $HOST:$PORT"
else fail "not listening on $HOST:$PORT"; fi
if ss -ltnH "sport = :$PORT" | grep -qE '(0\.0\.0\.0|\*|\[::\]):'"$PORT"; then
  fail "port $PORT is bound on all interfaces (bypasses the reverse proxy)"
fi

echo "HTTP"
code="$(http_code "http://$HOST:$PORT/.well-known/oauth-authorization-server")"
[[ "$code" == 200 ]] && pass "local OAuth metadata ($code)" || fail "local OAuth metadata ($code)"
code="$(http_code "$PUBLIC_URL/.well-known/oauth-authorization-server")"
[[ "$code" == 200 ]] && pass "public OAuth metadata via TLS proxy ($code)" || fail "public OAuth metadata via TLS proxy ($code)"
code="$(http_code -X POST "$PUBLIC_URL/mcp")"
[[ "$code" == 401 ]] && pass "unauthenticated /mcp rejected ($code)" || fail "unauthenticated /mcp expected 401, got $code"

echo "MCP (authenticated)"
"$PY" - "$APP_DIR/oauth_state.json" > "$tmp/auth" <<'EOF'
import json, sys, time
try:
    tokens = json.load(open(sys.argv[1])).get("access_tokens", {}).values()
except FileNotFoundError:
    tokens = []
valid = [t for t in tokens if not t.get("expires_at") or t["expires_at"] > time.time() + 60]
if valid:
    print("Authorization: Bearer " + max(valid, key=lambda t: t.get("expires_at") or 0)["token"])
EOF
if [[ ! -s "$tmp/auth" ]]; then
  skip "no unexpired access token in oauth_state.json (connect Claude once, then re-run)"
else
  mcp_post() {
    curl -s -m 30 -o "$tmp/body" -w '%{http_code}' -X POST "$PUBLIC_URL/mcp" -H @"$tmp/auth" \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$1"
  }

  code="$(mcp_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"healthcheck","version":"0"}}}')"
  if [[ "$code" == 200 ]] && grep -q '"serverInfo":{"name":"t212-mcp"' "$tmp/body"; then pass "initialize ($code)"
  else fail "initialize ($code): $(head -c 200 "$tmp/body")"; fi

  code="$(mcp_post '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
  missing=""
  for tool in get_account_summary get_portfolio get_account_cash get_order_history get_dividends; do
    grep -q "\"name\":\"$tool\"" "$tmp/body" || missing+=" $tool"
  done
  if [[ "$code" == 200 && -z "$missing" ]]; then pass "tools/list returns all 5 tools"
  else fail "tools/list ($code), missing:${missing:- none}"; fi

  if (( CALL_T212 )); then
    code="$(mcp_post '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_account_cash","arguments":{}}}')"
    if [[ "$code" == 200 ]] && grep -q '"result"' "$tmp/body" && ! grep -q '"isError":true' "$tmp/body"; then
      pass "tools/call get_account_cash reached the T212 API"
    else
      fail "tools/call get_account_cash ($code): $(grep -o '"text":"[^"]\{0,200\}' "$tmp/body" | head -1)"
    fi
  else
    skip "T212 API call (--no-t212)"
  fi
fi

echo
if (( failures )); then echo "$failures check(s) failed"; exit 1; fi
echo "All checks passed"
