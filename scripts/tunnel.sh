#!/usr/bin/env bash
# Expose the FlipCurve backend on this laptop to the internet through Cloudflare Tunnel.
#
#   scripts/tunnel.sh quick   # test: random https://xxxx.trycloudflare.com address, no login needed
#   scripts/tunnel.sh setup   # once: log in to Cloudflare (browser opens), create the "flipcurve"
#                             #       tunnel, point api.lewisdn.com at it
#   scripts/tunnel.sh run     # every time: start the backend and the named tunnel together
#
# Needs: brew install cloudflared
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=flipcurve
HOSTS=(api.lewisdn.com)
PORT=${PORT:-8000}
CONF="$HOME/.cloudflared/$NAME.yml"

command -v cloudflared >/dev/null || { echo "cloudflared missing: brew install cloudflared"; exit 1; }

start_backend() {
  if curl -fs "http://localhost:$PORT/api/v1/health" >/dev/null; then
    echo "backend already running on :$PORT"
  else
    echo "starting backend on :$PORT (log: backend/db/server.log)"
    mkdir -p backend/db
    uvicorn backend.main:app --host 127.0.0.1 --port "$PORT" > backend/db/server.log 2>&1 &
    BACKEND_PID=$!
    trap 'kill $BACKEND_PID 2>/dev/null' EXIT
    for _ in $(seq 30); do curl -fs "http://localhost:$PORT/api/v1/health" >/dev/null && break; sleep 0.5; done
  fi
}

case "${1:-}" in
  quick)
    start_backend
    echo "Open the https://....trycloudflare.com address it prints (it goes straight to the FlipCurve page)"
    cloudflared tunnel --url "http://localhost:$PORT"
    ;;
  setup)
    [ -f "$HOME/.cloudflared/cert.pem" ] || cloudflared tunnel login   # browser: pick lewisdn.com
    cloudflared tunnel info "$NAME" >/dev/null 2>&1 || cloudflared tunnel create "$NAME"
    ID=$(cloudflared tunnel list -o json | python3 -c "import json,sys; print([t['id'] for t in json.load(sys.stdin) if t['name']=='$NAME'][0])")
    for h in "${HOSTS[@]}"; do cloudflared tunnel route dns "$NAME" "$h" || true; done
    {
      echo "tunnel: $ID"
      echo "credentials-file: $HOME/.cloudflared/$ID.json"
      echo "ingress:"
      for h in "${HOSTS[@]}"; do echo "  - hostname: $h"; echo "    service: http://localhost:$PORT"; done
      echo "  - service: http_status:404"
    } > "$CONF"
    echo "Wrote $CONF. Now run: scripts/tunnel.sh run"
    ;;
  run)
    [ -f "$CONF" ] || { echo "run 'scripts/tunnel.sh setup' first"; exit 1; }
    start_backend
    echo "API live at https://api.lewisdn.com for https://lewisdn.com/flipcurve/. Ctrl-C to stop."
    cloudflared tunnel --config "$CONF" run "$NAME"
    ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
