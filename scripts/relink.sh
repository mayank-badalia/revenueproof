#!/usr/bin/env bash
#
# Point the deployed frontend at this machine, in one command.
#
#   ./scripts/relink.sh
#
# A free Cloudflare quick tunnel gets a new random hostname every time it starts, and
# Next.js compiles NEXT_PUBLIC_API_BASE into the bundle at build time — so a new
# hostname means a new Vercel build, every time, or the deployed site keeps calling an
# address that no longer exists. That is the loop this script closes.
#
# It also waits before touching DNS. Querying a quick-tunnel hostname in the seconds
# after it is created returns NXDOMAIN, and Cloudflare's SOA sets a **30 minute**
# negative cache TTL — so one impatient lookup makes the tunnel unreachable for half an
# hour even though it is working perfectly. That has bitten this project three times.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s%s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
die()  { printf '  %s✗%s %s\n' "$RED" "$OFF" "$1" >&2; exit 1; }

TUNNEL_LOG=/tmp/revenueproof-tunnel.log
DNS_WAIT="${DNS_WAIT:-100}"

# --- the API has to be up before a tunnel to it means anything ----------------
say "Checking the API"
curl -fsS --max-time 5 http://localhost:8000/health >/dev/null 2>&1 \
  || die "Nothing is answering on localhost:8000. Start it first: ./scripts/serve-demo.sh"
ok "API healthy on :8000"

# --- replace the tunnel -------------------------------------------------------
say "Opening a tunnel"
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2
nohup cloudflared tunnel --url http://localhost:8000 --no-autoupdate > "$TUNNEL_LOG" 2>&1 &

PUBLIC_URL=""
for _ in $(seq 1 45); do
  PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)
  [ -n "$PUBLIC_URL" ] && break
  sleep 1
done
[ -n "$PUBLIC_URL" ] || { tail -20 "$TUNNEL_LOG"; die "The tunnel never reported a URL."; }
ok "Tunnel registered: ${PUBLIC_URL}"

# --- wait for DNS *before* asking for it --------------------------------------
printf '  %swaiting %ss for Cloudflare to publish the hostname (do not skip: a lookup\n  now would cache NXDOMAIN for 30 minutes)%s\n' "$DIM" "$DNS_WAIT" "$OFF"
sleep "$DNS_WAIT"

curl -fsS --max-time 25 "${PUBLIC_URL}/health" >/dev/null \
  || die "${PUBLIC_URL}/health did not answer. Wait a minute and re-run."
ok "Tunnel is serving the API"

# --- point Vercel at it and rebuild -------------------------------------------
say "Repointing the deployed frontend"
cd frontend
npx vercel env rm NEXT_PUBLIC_API_BASE production --yes >/dev/null 2>&1 || true
printf '%s' "$PUBLIC_URL" | npx vercel env add NEXT_PUBLIC_API_BASE production >/dev/null
ok "NEXT_PUBLIC_API_BASE set"

npx vercel --prod --yes >/dev/null
ok "Deployed"
cd "$ROOT"

# --- and let the API accept the browser that will call it ---------------------
if ! grep -q "^EXTRA_ALLOWED_ORIGINS=.*vercel.app" .env 2>/dev/null; then
  printf '  %s!%s Add your Vercel URL to EXTRA_ALLOWED_ORIGINS in .env and restart the API,\n    or the browser will block every request.\n' "$RED" "$OFF"
fi

cat <<EOF

${BOLD}Ready.${OFF}

  Public API   ${GREEN}${PUBLIC_URL}${OFF}
  Frontend     https://revenueproof.vercel.app

${DIM}This URL lasts until the tunnel restarts. Re-run this script when it does.
To stop re-running it, give the backend a permanent home — see README, Deployment.${OFF}
EOF
