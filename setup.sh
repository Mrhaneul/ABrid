#!/usr/bin/env bash
# One-time setup on node 1 (spark-f135), after creating the GitHub OAuth app (see README.md, step 1).
#
#   bash setup.sh <github-client-id> <github-client-secret> "Mrhaneul,<kip-github-login>"
#
# Writes .env (mode 600), starts the container, and publishes it with Tailscale Funnel on
# https://spark-f135.saga-bramble.ts.net:10000 (port 10000 only; the existing 443 and 8443 setups are untouched).
# Re-running it updates .env and restarts; the message history in ./data is kept.
set -euo pipefail
cd "$(dirname "$0")"
ID="${1:?GitHub OAuth client id}"; SECRET_GH="${2:?GitHub OAuth client secret}"; USERS="${3:?allowed GitHub logins, comma-separated}"
HOST="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
URL="https://${HOST}:10000"
OLD_SECRET="$(grep -s '^BRIDGE_SECRET=' .env | cut -d= -f2- || true)"
umask 077
cat > .env <<EOF
BRIDGE_PUBLIC_URL=${URL}
GITHUB_CLIENT_ID=${ID}
GITHUB_CLIENT_SECRET=${SECRET_GH}
BRIDGE_ALLOWED_USERS=${USERS}
BRIDGE_SECRET=${OLD_SECRET:-$(openssl rand -hex 32)}
EOF
mkdir -p data
docker compose up -d --build
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8770/healthz >/dev/null && break; sleep 1; done
curl -sf http://127.0.0.1:8770/healthz >/dev/null || { docker compose logs --tail 30; exit 1; }
tailscale funnel --bg --https=10000 http://127.0.0.1:8770
sleep 2
curl -sf "${URL}/healthz" >/dev/null && echo "Public URL works: ${URL}" || echo "WARNING: ${URL}/healthz not reachable yet (Funnel DNS can take a minute)"
echo
echo "Web page (people):  ${URL}/"
echo "MCP URL (agents):   ${URL}/mcp"
echo "Allowed users:      ${USERS}"
