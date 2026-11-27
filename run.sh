#!/bin/sh
# Start Stock Overview on this computer and open it in your browser.
#   ./run.sh                 http://127.0.0.1:8000, reachable from this computer only
#   PORT=9000 ./run.sh       another port
#   HOST=0.0.0.0 ./run.sh    also reachable from your phone on the same Wi-Fi
#                            (set DASHBOARD_PASSWORD in .env first, see SETUP.md)
#   ./run.sh claude          show how to connect Claude to your data (MCP.md)
#   ./run.sh update          get the newest version (your data folder is never touched)
set -e
cd "$(dirname "$0")"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
REPO_PAGE="https://github.com/GidonPeeper/stock-overview"

if [ "$1" = "update" ]; then
  # git only changes the app's own files; data/ and .env are yours and stay as they are.
  if ! command -v git >/dev/null 2>&1; then
    echo "Cannot update: the git program is not installed on this computer."
    echo "Install it from https://git-scm.com/downloads and run ./run.sh update again,"
    echo "or download the newest version from $REPO_PAGE and copy your data folder"
    echo "(and .env, if you have one) into the new folder."
    exit 1
  fi
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "Cannot update: this folder was not downloaded with git (it has no .git folder)."
    echo "Download the newest version from $REPO_PAGE, then copy your data folder"
    echo "(and .env, if you have one) into the new folder. See TROUBLESHOOTING.md."
    exit 1
  fi
  CHANGED="$(git status --porcelain --untracked-files=no)"
  if [ -n "$CHANGED" ]; then
    echo "Cannot update: these files of the app were changed on this computer:"
    echo "$CHANGED" | sed 's/^/  /'
    echo "If you did not change them on purpose, set the changes aside and update:"
    echo "  git stash        (git stash pop brings them back)"
    echo "  ./run.sh update"
    echo "Your data folder and .env are not affected either way."
    exit 1
  fi
  BEFORE="$(git rev-parse --short HEAD)"
  echo "Looking for a newer version..."
  if ! git pull --ff-only --quiet; then
    echo
    echo "Could not update; nothing was changed. Git's message is above. Usually it is one of:"
    echo "  - no internet connection: connect, then run ./run.sh update again;"
    echo "  - \"no tracking information\": this copy is on another branch; run  git checkout main  first;"
    echo "  - \"Not possible to fast-forward\": this copy has its own commits; ask whoever made them."
    echo "More help: TROUBLESHOOTING.md (\"Updating\")."
    exit 1
  fi
  AFTER="$(git rev-parse --short HEAD)"
  if [ "$BEFORE" = "$AFTER" ]; then
    echo "Already up to date (version $AFTER)."
  else
    echo "Updated from version $BEFORE to $AFTER. What changed:"
    git log --no-decorate --format='  %s' "$BEFORE..$AFTER" | head -15
  fi
fi

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
  echo "Installing what the app needs (about a minute)..."
  if ! .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt; then
    echo
    echo "Installing what the app needs failed (the message is above). Check the internet"
    echo "connection and run this again. If it keeps failing, see TROUBLESHOOTING.md."
    exit 1
  fi
  echo "$WANT" > .venv/.requirements
fi

if [ "$1" = "update" ]; then
  echo "Done. If Stock Overview is running, stop it (Ctrl+C in its window) and start it again: ./run.sh"
  exit 0
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
# Something already answers on this port (often Stock Overview itself, started
# earlier): say so plainly instead of failing with a Python error.
if .venv/bin/python -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(s.connect_ex(('127.0.0.1', int(sys.argv[1]))) != 0)" "$PORT" 2>/dev/null; then
  echo "Port $PORT is already in use. Stock Overview may already be running: open $URL"
  echo "Or start this copy on another port:  PORT=$((PORT + 1)) ./run.sh"
  exit 1
fi
echo "Stock Overview: $URL   (stop with Ctrl+C)"
if [ -z "$NO_BROWSER" ]; then
  ( sleep 3
    if command -v open >/dev/null 2>&1; then open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
    fi ) >/dev/null 2>&1 &
fi
exec .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port "$PORT" --log-level warning
