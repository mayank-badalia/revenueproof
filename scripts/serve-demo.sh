#!/usr/bin/env bash
#
# Bring the whole stack up and expose it to the internet for a demonstration.
#
#   ./scripts/serve-demo.sh
#
# Starts Docker (Postgres, Redis, Neo4j, ChromaDB), the FastAPI backend, and a
# Cloudflare tunnel, then prints the public URL to paste into Vercel.
#
# Why a tunnel rather than a serverless deployment: this backend is 619 MB of
# Python (PyMuPDF, SciPy, OR-Tools, scikit-learn) against Vercel's 250 MB
# function limit, it needs four stateful services, the live trace is a WebSocket,
# and a full verification run takes 47-113 seconds. None of that fits a serverless
# function, and pretending otherwise would produce a demo that fails in front of
# whoever is watching.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s%s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
die()  { printf '  %s✗%s %s\n' "$RED" "$OFF" "$1" >&2; exit 1; }

# --- prerequisites -----------------------------------------------------------
say "Checking prerequisites"
command -v docker >/dev/null || die "Docker is not installed. On macOS: brew install colima docker && colima start --cpu 4 --memory 8"
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Try: colima start --cpu 4 --memory 8"
ok "Docker is running"

[ -f .env ] || die ".env is missing. Copy it: cp .env.example .env  (then add CEREBRAS_API_KEY)"
grep -q "^CEREBRAS_API_KEY=.\+" .env || printf '  %s!%s No CEREBRAS_API_KEY in .env — contract reading and the critic will be skipped, every financial figure still computes.\n' "$RED" "$OFF"
ok ".env present"

command -v cloudflared >/dev/null || die "cloudflared is not installed. Run: brew install cloudflared"
ok "cloudflared is installed"

# --- infrastructure ----------------------------------------------------------
say "Starting infrastructure"
docker compose -f infra/docker-compose.yml up -d >/dev/null
for i in $(seq 1 60); do
  if docker compose -f infra/docker-compose.yml exec -T postgres pg_isready -U revenueproof >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && die "PostgreSQL did not become ready in 60s"
  sleep 1
done
ok "PostgreSQL, Redis, Neo4j and ChromaDB are up"

# --- backend -----------------------------------------------------------------
say "Starting the API"
cd backend
[ -d .venv ] || die "backend/.venv is missing. Run: cd backend && uv venv --python 3.13 && uv pip install -e '.[dev]'"

# --reload is deliberately absent: editing a file mid-demo restarts the process
# and the browser reports the dropped request as a CORS error, which sends
# everyone hunting for a CORS problem that does not exist.
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/revenueproof-api.log 2>&1 &
API_PID=$!
cd "$ROOT"

for i in $(seq 1 60); do
  if curl -fsS localhost:8000/health >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && { cat /tmp/revenueproof-api.log; die "The API did not start. Log above."; }
  sleep 1
done
ok "API healthy on :8000  ${DIM}(log: /tmp/revenueproof-api.log)${OFF}"

# --- tunnel ------------------------------------------------------------------
say "Opening the public tunnel"
cloudflared tunnel --url http://localhost:8000 --no-autoupdate > /tmp/revenueproof-tunnel.log 2>&1 &
TUNNEL_PID=$!

PUBLIC_URL=""
for i in $(seq 1 45); do
  PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/revenueproof-tunnel.log 2>/dev/null | head -1 || true)
  [ -n "$PUBLIC_URL" ] && break
  sleep 1
done
[ -n "$PUBLIC_URL" ] || { cat /tmp/revenueproof-tunnel.log; die "The tunnel did not report a URL. Log above."; }

curl -fsS "$PUBLIC_URL/health" >/dev/null 2>&1 && ok "Tunnel is serving the API" || die "The tunnel opened but $PUBLIC_URL/health did not answer"

cleanup() {
  printf '\n%sShutting down%s\n' "$BOLD" "$OFF"
  kill "$TUNNEL_PID" "$API_PID" 2>/dev/null || true
  wait "$TUNNEL_PID" "$API_PID" 2>/dev/null || true
  printf '  Docker is still running. Stop it with: docker compose -f infra/docker-compose.yml down\n'
}
trap cleanup EXIT INT TERM

cat <<EOF

${BOLD}Ready.${OFF}

  Public API   ${GREEN}${PUBLIC_URL}${OFF}
  Local UI     http://localhost:3000

${BOLD}To point the deployed frontend at this machine:${OFF}

  vercel env rm NEXT_PUBLIC_API_BASE production --yes 2>/dev/null || true
  echo "${PUBLIC_URL}" | vercel env add NEXT_PUBLIC_API_BASE production
  vercel --prod

${BOLD}And let this API accept requests from the deployed frontend${OFF} — add your
Vercel URL to EXTRA_ALLOWED_ORIGINS in .env, then re-run this script:

  EXTRA_ALLOWED_ORIGINS=https://your-app.vercel.app

The URL above changes every time this script runs, because it is a free ad-hoc
tunnel. Re-run the two vercel commands whenever it does.

${DIM}Ctrl-C to stop the API and the tunnel.${OFF}
EOF

wait "$API_PID"
