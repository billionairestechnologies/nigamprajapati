#!/usr/bin/env bash
# One-click launcher: sets up the venv on first run, then starts the dashboard.
# Works on macOS, Linux (including a headless VPS over SSH), inside a plain
# terminal or double-clicked via run.command (macOS).
set -uo pipefail
cd "$(dirname "$0")/backend"

FAILED=0
trap 'FAILED=1' ERR

fail() {
  echo ""
  echo "ERROR: $1" >&2
  FAILED=1
}

pause_if_interactive() {
  if [ -t 0 ]; then
    echo ""
    read -r -p "Press Enter to close this window..." _ || true
  fi
}

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 is not installed or not on PATH. Install Python 3.10+ from python.org, then run this again."
  pause_if_interactive
  exit 1
fi

PYVER=$(python3 -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || echo "unknown")
echo "Using python3 ($PYVER) at $(command -v python3)"

if [ ! -d ".venv" ]; then
  echo "Setting up Python environment (first run only)..."
  if ! python3 -m venv .venv; then
    fail "Could not create the virtual environment. Check the python3 install above."
    pause_if_interactive
    exit 1
  fi
  if ! ./.venv/bin/pip install --upgrade pip -q; then
    fail "Could not upgrade pip. Check your internet connection."
    pause_if_interactive
    exit 1
  fi
  if ! ./.venv/bin/pip install -r requirements.txt; then
    fail "Dependency install failed - see the pip output above for which package broke."
    pause_if_interactive
    exit 1
  fi
  echo "Dependencies installed."
fi

if [ ! -f ".env" ]; then
  echo "No .env found - creating one with a fresh encryption key."
  cp .env.example .env
  KEY=$(./.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  # sed's in-place flag differs between BSD/macOS sed and GNU/Linux sed -
  # try the macOS form first, fall back to the Linux form so this works on
  # both a Mac and a Linux VPS.
  if sed -i '' "s/^APP_SECRET_KEY=.*/APP_SECRET_KEY=${KEY}/" .env 2>/dev/null; then
    :
  else
    sed -i "s/^APP_SECRET_KEY=.*/APP_SECRET_KEY=${KEY}/" .env
  fi
  echo "Edit backend/.env to set DASHBOARD_USERNAME / DASHBOARD_PASSWORD, then re-run this script."
fi

if [ "$FAILED" -ne 0 ]; then
  fail "Setup did not complete cleanly - see messages above before trusting this run."
  pause_if_interactive
  exit 1
fi

# Respects HOST/PORT from .env if you've set them (e.g. HOST=0.0.0.0 to
# reach the dashboard remotely on a VPS) - defaults to loopback-only, which
# is the safe choice for a single-machine app holding broker credentials
# and doing HTTP Basic Auth (no TLS) over plain HTTP.
HOST=$(grep -E '^HOST=' .env 2>/dev/null | cut -d= -f2)
HOST=${HOST:-127.0.0.1}
PORT=$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2)
PORT=${PORT:-8787}

if command -v lsof >/dev/null 2>&1 && lsof -i ":${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "Port ${PORT} is already in use - the dashboard may already be running at http://${HOST}:${PORT}, or another app is using that port."
  pause_if_interactive
  exit 1
fi

if [ "$HOST" = "0.0.0.0" ]; then
  echo "WARNING: HOST=0.0.0.0 in .env - this dashboard will be reachable from"
  echo "outside this machine over plain HTTP (no TLS). Only do this behind a"
  echo "firewall/VPN you trust - it holds your broker credentials."
fi

echo "Starting dashboard at http://${HOST}:${PORT} ..."

# Best-effort auto-open the dashboard in the default browser once it's up.
(
  sleep 2
  OPEN_URL="http://127.0.0.1:${PORT}"
  if command -v open >/dev/null 2>&1; then
    open "$OPEN_URL" 2>/dev/null || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$OPEN_URL" 2>/dev/null || true
  fi
) &

./.venv/bin/python -m uvicorn app.main:app --host "${HOST}" --port "${PORT}"
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
  fail "The dashboard server exited with an error (exit code ${STATUS}) - see the output above."
  pause_if_interactive
  exit "$STATUS"
fi
