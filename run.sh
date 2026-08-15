#!/usr/bin/env bash
#
#   ./run.sh
#
# One command. Brings up everything RevenueProof needs and leaves it running:
# Docker (Postgres, Redis, Neo4j, ChromaDB), the FastAPI backend, the Next.js UI,
# a public Cloudflare tunnel, and — only when it has actually gone stale — the
# deployed frontend at https://revenueproof.vercel.app.
#
#   ./run.sh                 everything, and repoint Vercel only if it is stale
#   ./run.sh --local         skip the tunnel and Vercel; localhost only
#   ./run.sh --no-deploy      open the tunnel, but never touch Vercel
#   ./run.sh --fresh-tunnel  force a new tunnel hostname (see the warning below)
#   ./run.sh --stop          shut everything down, including Docker
#
# Safe to run twice. Every step checks whether the thing is already up and adopts
# it rather than starting a second copy — running this while it is already running
# is a no-op that re-prints the URLs, not a way to get two backends fighting over
# port 8000.
#
# ---------------------------------------------------------------------------
# The one thing worth understanding before you use --fresh-tunnel
#
# A free quick tunnel gets a random hostname, and Cloudflare publishes it in DNS a
# few seconds AFTER cloudflared prints it. trycloudflare.com's SOA sets a 30 minute
# negative-cache TTL, so a single lookup in that window caches NXDOMAIN on this Mac
# for half an hour — the tunnel serves the whole internet fine and this machine
# alone cannot see it. That has cost this project several hours across four separate
# occasions.
#
# So: this script reuses a healthy tunnel whenever one exists, waits before its
# first lookup when it has to mint one, and falls back to 1.1.1.1 before it will
# call a tunnel dead. Reach for --fresh-tunnel only when the current one is really
# broken, and expect to wait.
# ---------------------------------------------------------------------------

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '\n%s%s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
skip() { printf '  %s·%s %s%s%s\n' "$DIM" "$OFF" "$DIM" "$1" "$OFF"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
die()  { printf '  %s✗%s %s\n' "$RED" "$OFF" "$1" >&2; exit 1; }

# Start something in the background and actually come back.
#
#   start_bg <logfile> <dir> <command...>
#
# Every redirection here is load-bearing. A background child that inherits this
# script's stdin or stdout keeps those descriptors open for as long as it lives —
# so `./run.sh | tee setup.log` would sit there showing nothing until the backend
# was killed, and the script would look hung when it had in fact finished. The
# child gets /dev/null on stdin and its own log on stdout and stderr; the subshell
# gets /dev/null on all three.
start_bg() {
  local log="$1" dir="$2"; shift 2
  ( cd "$dir" && "$@" >"$log" 2>&1 </dev/null & ) </dev/null >/dev/null 2>&1
}

API_LOG=/tmp/revenueproof-api.log
UI_LOG=/tmp/revenueproof-ui.log
TUNNEL_LOG=/tmp/revenueproof-tunnel.log
DEPLOY_LOG=/tmp/revenueproof-deploy.log
DNS_WAIT="${DNS_WAIT:-100}"
COMPOSE="infra/docker-compose.yml"
FRONTEND_URL="https://revenueproof.vercel.app"

WANT_TUNNEL=1
WANT_DEPLOY=1
FRESH_TUNNEL=""
for arg in "$@"; do
  case "$arg" in
    --local)        WANT_TUNNEL=""; WANT_DEPLOY="" ;;
    --no-deploy)    WANT_DEPLOY="" ;;
    --fresh-tunnel) FRESH_TUNNEL=1 ;;
    --stop)         MODE_STOP=1 ;;
    -h|--help)      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              die "Unknown option: $arg  (try --help)" ;;
  esac
done

# --- --stop ------------------------------------------------------------------
if [ -n "${MODE_STOP:-}" ]; then
  say "Shutting down"

  # Kill by port, not by process name. `next dev` forks a `next-server` child that
  # is the process actually holding :3000 — pattern-matching "next dev" leaves it
  # running, and a stale Next server goes on serving an old bundle from a port you
  # believe is free. Whatever holds the port is the thing to stop.
  kill_port() {
    local port="$1" label="$2" pids
    pids=$(lsof -ti "tcp:$port" 2>/dev/null || true)
    [ -n "$pids" ] || { skip "Nothing on :$port ($label)"; return; }
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    for _ in $(seq 1 10); do
      lsof -ti "tcp:$port" >/dev/null 2>&1 || break
      sleep 1
    done
    # shellcheck disable=SC2086
    lsof -ti "tcp:$port" >/dev/null 2>&1 && kill -9 $(lsof -ti "tcp:$port") 2>/dev/null || true
    ok "$label stopped (:$port)"
  }

  pkill -f "cloudflared tunnel --url http://localhost:8000" 2>/dev/null && ok "Tunnel stopped" || skip "No tunnel running"
  kill_port 8000 "API"
  kill_port 3000 "UI"
  docker compose -f "$COMPOSE" down >/dev/null 2>&1 && ok "Docker stopped" || skip "Docker was not running"
  printf '\n'
  exit 0
fi

# --- prerequisites -----------------------------------------------------------
say "Checking prerequisites"
command -v docker >/dev/null || die "Docker is not installed. On macOS: brew install colima docker && colima start --cpu 4 --memory 8"
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Try: colima start --cpu 4 --memory 8"
ok "Docker is running"

[ -f .env ] || die ".env is missing. Copy it: cp .env.example .env  (then add CEREBRAS_API_KEY)"
grep -q "^CEREBRAS_API_KEY=.\+" .env \
  || warn "No CEREBRAS_API_KEY in .env — contract reading and the critic will be skipped. Every financial figure still computes."
ok ".env present"

[ -d backend/.venv ] || die "backend/.venv is missing. Run: cd backend && uv venv --python 3.13 && uv pip install -e '.[dev]'"
[ -d frontend/node_modules ] || die "frontend/node_modules is missing. Run: cd frontend && npm install"
ok "Backend venv and frontend packages present"

if [ -n "$WANT_TUNNEL" ]; then
  command -v cloudflared >/dev/null || die "cloudflared is not installed. Run: brew install cloudflared"
  ok "cloudflared is installed"
fi

# --- infrastructure ----------------------------------------------------------
say "Starting infrastructure"
docker compose -f "$COMPOSE" up -d >/dev/null
for i in $(seq 1 60); do
  docker compose -f "$COMPOSE" exec -T postgres pg_isready -U revenueproof >/dev/null 2>&1 && break
  [ "$i" = 60 ] && die "PostgreSQL did not become ready in 60s"
  sleep 1
done
ok "PostgreSQL, Redis, Neo4j and ChromaDB are up"

# --- backend -----------------------------------------------------------------
say "Starting the API"
if curl -fsS --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
  skip "Already healthy on :8000 — adopting it"
else
  # --reload is deliberately absent: editing a file mid-demo restarts the process,
  # and the browser reports the dropped request as a CORS error, which sends
  # everyone hunting for a CORS problem that does not exist.
  start_bg "$API_LOG" backend .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  for i in $(seq 1 60); do
    curl -fsS --max-time 5 http://localhost:8000/health >/dev/null 2>&1 && break
    [ "$i" = 60 ] && { tail -30 "$API_LOG"; die "The API did not start. Log above, full log: $API_LOG"; }
    sleep 1
  done
  ok "API healthy on :8000  ${DIM}(log: $API_LOG)${OFF}"
fi

# --- frontend ----------------------------------------------------------------
say "Starting the UI"
if curl -fsS --max-time 5 http://localhost:3000 >/dev/null 2>&1; then
  skip "Already serving on :3000 — adopting it"
else
  start_bg "$UI_LOG" frontend npm run dev -- -p 3000
  for i in $(seq 1 90); do
    curl -fsS --max-time 5 http://localhost:3000 >/dev/null 2>&1 && break
    [ "$i" = 90 ] && { tail -30 "$UI_LOG"; die "The UI did not start. Log above, full log: $UI_LOG"; }
    sleep 1
  done
  ok "UI serving on :3000  ${DIM}(log: $UI_LOG)${OFF}"
fi

if [ -z "$WANT_TUNNEL" ]; then
  cat <<EOF

${BOLD}Ready — local only.${OFF}

  UI    ${GREEN}http://localhost:3000${OFF}
  API   http://localhost:8000

${DIM}Stop everything with: ./run.sh --stop${OFF}
EOF
  exit 0
fi

# --- tunnel ------------------------------------------------------------------
# Reuse before replace. A healthy tunnel is worth more than a fresh one: minting a
# new hostname costs 100 seconds of DNS wait, a Vercel rebuild, and a shot at the
# 30-minute negative-cache trap described at the top of this file.
say "Opening the public tunnel"

tunnel_host_from_log() {
  grep -aoE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true
}

# Answers on this machine's resolver, or — failing that — on 1.1.1.1? The tunnel's
# consumer is the deployed frontend, so a stale mDNSResponder entry here is a local
# inconvenience and not a dead tunnel. Sets RESOLVER_NOTE when only 1.1.1.1 worked.
RESOLVER_NOTE=""
tunnel_answers() {
  local url="$1" host ip
  RESOLVER_NOTE=""
  curl -fsS --max-time 20 "$url/health" >/dev/null 2>&1 && return 0
  host="${url#https://}"
  ip=$(dig +short "$host" @1.1.1.1 2>/dev/null | grep -E '^[0-9.]+$' | head -1 || true)
  [ -n "$ip" ] || return 1
  curl -fsS --max-time 20 --resolve "$host:443:$ip" "$url/health" >/dev/null 2>&1 || return 1
  RESOLVER_NOTE="public"
  return 0
}

PUBLIC_URL=""
if [ -z "$FRESH_TUNNEL" ] && pgrep -f "cloudflared tunnel --url http://localhost:8000" >/dev/null 2>&1; then
  CANDIDATE=$(tunnel_host_from_log)
  if [ -n "$CANDIDATE" ] && tunnel_answers "$CANDIDATE"; then
    PUBLIC_URL="$CANDIDATE"
    skip "Reusing the tunnel already running — no new hostname, no DNS wait"
  else
    warn "A tunnel is running but is not serving the API. Replacing it."
  fi
fi

if [ -z "$PUBLIC_URL" ]; then
  pkill -f "cloudflared tunnel --url http://localhost:8000" 2>/dev/null || true
  sleep 2
  start_bg "$TUNNEL_LOG" "$ROOT" cloudflared tunnel --url http://localhost:8000 --no-autoupdate

  for _ in $(seq 1 45); do
    PUBLIC_URL=$(tunnel_host_from_log)
    [ -n "$PUBLIC_URL" ] && break
    sleep 1
  done
  [ -n "$PUBLIC_URL" ] || { tail -20 "$TUNNEL_LOG"; die "The tunnel never reported a URL. Log: $TUNNEL_LOG"; }
  ok "Tunnel registered: ${PUBLIC_URL}"

  printf '  %swaiting %ss for Cloudflare to publish the hostname (do not skip: a lookup\n  now would cache NXDOMAIN for 30 minutes)%s\n' "$DIM" "$DNS_WAIT" "$OFF"
  sleep "$DNS_WAIT"

  TUNNEL_OK=""
  for _ in $(seq 1 6); do
    tunnel_answers "$PUBLIC_URL" && { TUNNEL_OK=1; break; }
    sleep 10
  done
  [ -n "$TUNNEL_OK" ] || die "The tunnel opened but $PUBLIC_URL/health did not answer, on this resolver or on 1.1.1.1"
fi

if [ "$RESOLVER_NOTE" = "public" ]; then
  ok "Tunnel is serving the API ${DIM}(confirmed via 1.1.1.1)${OFF}"
  warn "This Mac has NXDOMAIN cached for that hostname, so a browser *here* cannot reach"
  printf '    it. The deployed frontend is unaffected. To clear it:\n      %ssudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder%s\n' "$DIM" "$OFF"
else
  ok "Tunnel is serving the API"
fi

# --- the API has to accept the browser that will call it ---------------------
if ! grep -q "^EXTRA_ALLOWED_ORIGINS=.*vercel.app" .env 2>/dev/null; then
  warn "No vercel.app origin in EXTRA_ALLOWED_ORIGINS (.env). The deployed frontend's"
  printf '    requests will be blocked. Add %sEXTRA_ALLOWED_ORIGINS=%s%s and re-run.\n' "$DIM" "$FRONTEND_URL" "$OFF"
fi

# --- deploy, but only when the deployed bundle has actually gone stale --------
say "Checking the deployed frontend"
if [ -z "$WANT_DEPLOY" ]; then
  skip "--no-deploy: leaving Vercel alone"
else
  # NEXT_PUBLIC_* is compiled into the bundle at build time, so the only honest way
  # to ask "is the deployed site pointing at this tunnel" is to read the bundle.
  DEPLOYED_BASE=$(
    curl -fsS --max-time 20 "$FRONTEND_URL/" 2>/dev/null \
      | grep -o '/_next/static/[a-zA-Z0-9._/-]*\.js' | sort -u \
      | while read -r chunk; do curl -fsS --max-time 15 "$FRONTEND_URL$chunk" 2>/dev/null; done \
      | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | sort -u | head -1 || true
  )

  if [ "$DEPLOYED_BASE" = "$PUBLIC_URL" ]; then
    ok "${FRONTEND_URL} already points here — nothing to rebuild"
  else
    [ -n "$DEPLOYED_BASE" ] && printf '  %sdeployed bundle points at %s%s\n' "$DIM" "$DEPLOYED_BASE" "$OFF"
    printf '  repointing and rebuilding — this takes about a minute\n'
    # The Vercel CLI writes its whole build log to stderr, which would bury every
    # other line this script prints. It goes to a file and is shown only if the
    # deploy fails — where it is the first thing you need.
    (
      cd frontend
      npx vercel env rm NEXT_PUBLIC_API_BASE production --yes || true
      printf '%s' "$PUBLIC_URL" | npx vercel env add NEXT_PUBLIC_API_BASE production
      npx vercel --prod --yes
    ) > "$DEPLOY_LOG" 2>&1 \
      || { tail -30 "$DEPLOY_LOG"; die "The Vercel deploy failed. Log above, full log: $DEPLOY_LOG"; }
    ok "Deployed — ${FRONTEND_URL} now calls this machine  ${DIM}(log: $DEPLOY_LOG)${OFF}"
  fi
fi

cat <<EOF

${BOLD}Ready.${OFF}

  Local UI      ${GREEN}http://localhost:3000${OFF}
  Deployed UI   ${GREEN}${FRONTEND_URL}${OFF}
  Public API    ${PUBLIC_URL}
  Local API     http://localhost:8000

${BOLD}Logs${OFF}   API $API_LOG · UI $UI_LOG · tunnel $TUNNEL_LOG

${DIM}Everything keeps running after this script exits — nothing is tied to this
terminal. Re-run ./run.sh any time to check the stack and repoint the deploy;
it adopts what is already up rather than starting a second copy.

Stop everything with: ./run.sh --stop${OFF}
EOF
