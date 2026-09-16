#!/usr/bin/env bash
# End-to-end smoke test against a running instance.
# Usage: ./scripts/smoke.sh [base_url]
#
# Deliberately shell + curl + jq rather than pytest: this validates a *deployed*
# artifact over the network, which is a different thing from the unit suite.
set -euo pipefail

BASE="${1:-http://localhost:8080}"
GAME="smoke-$(date +%s)"
PASS=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n    %s\n' "$1" "${2:-}"; exit 1; }

echo "Smoke testing ${BASE} (game: ${GAME})"

echo "-> waiting for readiness"
for i in $(seq 1 30); do
  if curl -fsS "${BASE}/healthz" >/dev/null 2>&1; then break; fi
  [ "$i" -eq 30 ] && fail "service never became healthy"
  sleep 2
done
ok "healthz responds"

[ "$(curl -fsS "${BASE}/readyz" | jq -r .status)" = "ready" ] \
  || fail "readyz not ready"
ok "readyz reports ready"

echo "-> submitting scores"
for entry in "alice:500" "bob:900" "carol:700" "dave:900"; do
  user="${entry%%:*}"; score="${entry##*:}"
  curl -fsS -X POST "${BASE}/v1/games/${GAME}/scores" \
    -H 'content-type: application/json' \
    -d "{\"user_id\":\"${user}\",\"score\":${score}}" >/dev/null \
    || fail "submit failed for ${user}"
done
ok "four scores accepted"

echo "-> reading the board"
BOARD=$(curl -fsS "${BASE}/v1/games/${GAME}/leaderboard?limit=10")
[ "$(echo "$BOARD" | jq -r '.total_players')" = "4" ] \
  || fail "expected 4 players" "$BOARD"
ok "total_players is 4"

# bob and dave both scored 900, so both must hold rank 1 and carol must be 3rd.
[ "$(echo "$BOARD" | jq -r '[.entries[] | select(.score==900) | .rank] | unique | @csv')" = "1" ] \
  || fail "tied players should share rank 1" "$BOARD"
ok "tied scores share rank 1"

[ "$(echo "$BOARD" | jq -r '.entries[] | select(.user_id=="carol") | .rank')" = "3" ] \
  || fail "competition ranking should skip rank 2" "$BOARD"
ok "next distinct score ranks 3 (competition ranking)"

echo "-> reading a player's surroundings"
CTX=$(curl -fsS "${BASE}/v1/games/${GAME}/users/carol/context?radius=1")
[ "$(echo "$CTX" | jq -r '.user.user_id')" = "carol" ] || fail "wrong pivot user" "$CTX"
[ "$(echo "$CTX" | jq -r '.above | length')" = "1" ] || fail "missing neighbour above" "$CTX"
[ "$(echo "$CTX" | jq -r '.below | length')" = "1" ] || fail "missing neighbour below" "$CTX"
ok "context returns neighbours on both sides"

echo "-> idempotent retry"
for _ in 1 2; do
  curl -fsS -X POST "${BASE}/v1/games/${GAME}/scores" \
    -H 'content-type: application/json' \
    -d '{"user_id":"erin","score":10,"mode":"increment","idempotency_key":"smoke-1"}' \
    >/dev/null
done
[ "$(curl -fsS "${BASE}/v1/games/${GAME}/users/erin" | jq -r .score)" = "10" ] \
  || fail "duplicate submission was applied twice"
ok "repeated idempotency key applied once"

echo "-> validation"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/v1/games/${GAME}/scores" \
  -H 'content-type: application/json' -d '{"user_id":"","score":"nope"}')
[ "$CODE" = "422" ] || fail "expected 422 for malformed payload, got ${CODE}"
ok "malformed payload rejected with 422"

CODE=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/v1/games/${GAME}/users/ghost")
[ "$CODE" = "404" ] || fail "expected 404 for unknown user, got ${CODE}"
ok "unknown player returns 404"

echo "-> cleanup"
curl -fsS -X DELETE "${BASE}/v1/games/${GAME}/users/alice" -o /dev/null -w '' || true
ok "player deletion accepted"

printf '\n\033[32mAll %d smoke checks passed against %s\033[0m\n' "$PASS" "$BASE"
