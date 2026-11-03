#!/bin/bash
# Stocker AI launcher for macOS. Double-click to start. First run installs dependencies (~2 min).
cd "$(dirname "$0")" || exit 1
echo "=== Stocker AI ==="

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is required. Download it from https://www.python.org/downloads/ and run this again."
  open "https://www.python.org/downloads/"
  read -r -p "Press Enter to close..."; exit 1
fi

if [ ! -x ".venv/bin/streamlit" ]; then
  echo "First run: setting up (this takes a couple of minutes)..."
  "$PY" -m venv .venv || { read -r -p "Setup failed. Press Enter to close..."; exit 1; }
  .venv/bin/python -m pip install --upgrade pip -q
fi
# install on first run, and again whenever a new version adds requirements
if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing components (needs internet)..."
  .venv/bin/python -m pip install -r requirements.txt -q || { read -r -p "Install failed (check internet). Press Enter to close..."; exit 1; }
  cp requirements.txt .venv/requirements.installed
fi

echo "Starting... your browser will open automatically"
echo "Keep this window open while using Stocker AI. Close it to stop."
.venv/bin/streamlit run app.py --server.headless false
