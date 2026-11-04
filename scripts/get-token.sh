#!/usr/bin/env bash
# Get a usable access token through the gateway.
# Registration returns a token, but the account has email_verified=false, and
# household routes answer 403 for unverified accounts. The lab has no mail
# server, so we mark the synthetic account verified directly in Postgres and
# log in again to get a token that carries the verified state.
# Usage: bash scripts/get-token.sh [base-url]   (default http://127.0.0.1:30080)
# Writes the token to evidence/token.txt.
set -euo pipefail
BASE="${1:-http://127.0.0.1:30080}"
NS=university
HERE="$(cd "$(dirname "$0")" && pwd)"
EVID="$(dirname "$HERE")/evidence"
mkdir -p "$EVID"

EMAIL="endterm$(date +%s)@lab.example"   # generated here: safe to inline in SQL
PASS="synthetic-pass-255193"

echo "1) register $EMAIL"
curl -s -o /dev/null -w '   HTTP %{http_code}\n' -X POST "$BASE/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"name\":\"Endterm User\"}"

echo "2) mark email verified (lab has no mail server)"
kubectl -n $NS exec postgres-0 -- sh -c \
  "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"UPDATE users SET email_verified=true WHERE email='$EMAIL'\""

echo "3) login"
RESP="$(curl -s -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")"
printf '%s' "$RESP" | python -c 'import sys,json; print(json.load(sys.stdin)["access_token"])' > "$EVID/token.txt"
echo "   token saved to evidence/token.txt"
