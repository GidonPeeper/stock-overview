#!/bin/sh
# Start Stock Overview on this computer and open it in your browser.
#   ./run.sh                 http://127.0.0.1:8000, reachable from this computer only
#   PORT=9000 ./run.sh       another port
#   HOST=0.0.0.0 ./run.sh    also reachable from your phone on the same Wi-Fi
#                            (set DASHBOARD_PASSWORD in .env first, see SETUP.md)
#   ./run.sh claude          show how to connect Claude to your data (MCP.md)
set -e
cd "$(dirname "$0")"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.10 or newer is needed."
    echo "Install it from https://www.python.org/downloads/ and run this again."
    exit 1
  fi
  echo "First run: setting up (about a minute)..."
  "$PY" -m venv .venv
fi

# Install what the app needs, again after an update changed requirements.txt.
WANT="$(cksum < requirements.txt)"
if [ "$WANT" != "$(cat .venv/.requirements 2>/dev/null)" ]; then
  .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
  echo "$WANT" > .venv/.requirements
fi

if [ "$1" = "claude" ]; then
  DIR="$(pwd)"
  cat <<TEXT
Connect Claude to Stock Overview (read-only; details in MCP.md).

Claude Code: run this once.

  claude mcp add stock-overview --scope user -e PYTHONPATH="$DIR" -- "$DIR/.venv/bin/python" -P -m backend.mcp_server

Claude Desktop: Settings > Developer > Edit Config, add this, then restart Claude.

  {
    "mcpServers": {
      "stock-overview": {
        "command": "$DIR/.venv/bin/python",
        "args": ["-P", "-m", "backend.mcp_server"],
        "env": { "PYTHONPATH": "$DIR" }
      }
    }
  }
TEXT
  exit 0
fi

URL="http://127.0.0.1:$PORT"
echo "Stock Overview: $URL   (stop with Ctrl+C)"
if [ -z "$NO_BROWSER" ]; then
  ( sleep 3
    if command -v open >/dev/null 2>&1; then open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
    fi ) >/dev/null 2>&1 &
fi
exec .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port "$PORT" --log-level warning
