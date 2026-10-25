#!/usr/bin/env bash
# Expose the FlipCurve backend on this laptop to the internet through Cloudflare Tunnel.
#
#   scripts/tunnel.sh quick   # test: random https://xxxx.trycloudflare.com address, no login needed
#   scripts/tunnel.sh setup   # once: log in to Cloudflare (browser opens), create the "flipcurve"
#                             #       tunnel, point api.lewisdn.com at it
#   scripts/tunnel.sh run     # every time: start the backend and the named tunnel together
#
# Needs cloudflared; if it isn't installed, the script downloads Cloudflare's own build into .bin/
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=flipcurve
HOSTS=(api.lewisdn.com)
PORT=${PORT:-8000}
CONF="$HOME/.cloudflared/$NAME.yml"

# cloudflared: use the installed one, else download Cloudflare's official build into .bin/ (no Homebrew needed)
if command -v cloudflared >/dev/null; then
  CF=cloudflared
else
  CF="$PWD/.bin/cloudflared"
  if [ ! -x "$CF" ]; then
    case "$(uname -s)-$(uname -m)" in
      Darwin-arm64) ASSET=cloudflared-darwin-arm64.tgz ;;
      Darwin-x86_64) ASSET=cloudflared-darwin-amd64.tgz ;;
      Linux-x86_64) ASSET=cloudflared-linux-amd64 ;;
      Linux-aarch64) ASSET=cloudflared-linux-arm64 ;;
      *) echo "Unsupported system $(uname -sm); install cloudflared yourself"; exit 1 ;;
    esac
    echo "Downloading cloudflared ($ASSET) from github.com/cloudflare/cloudflared ..."
    mkdir -p .bin
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/$ASSET"
    if [[ "$ASSET" == *.tgz ]]; then
      curl -fsSL "$URL" | tar -xz -C .bin cloudflared
    else
      curl -fsSL -o "$CF" "$URL"
    fi
    chmod +x "$CF"
  fi
fi
"$CF" --version

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
    "$CF" tunnel --url "http://localhost:$PORT"
    ;;
  setup)
    [ -f "$HOME/.cloudflared/cert.pem" ] || "$CF" tunnel login   # browser: pick lewisdn.com
    "$CF" tunnel info "$NAME" >/dev/null 2>&1 || "$CF" tunnel create "$NAME"
    ID=$("$CF" tunnel list -o json | python3 -c "import json,sys; print([t['id'] for t in json.load(sys.stdin) if t['name']=='$NAME'][0])")
    for h in "${HOSTS[@]}"; do "$CF" tunnel route dns "$NAME" "$h" || true; done
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
    "$CF" tunnel --config "$CONF" run "$NAME"
    ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
