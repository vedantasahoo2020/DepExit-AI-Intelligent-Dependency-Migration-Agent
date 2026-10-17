#!/usr/bin/env bash
# macOS / Linux: run `bash start-depexit.sh` (or double-click start-depexit.command on macOS).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/depexit ]; then
  echo "Setting up DepExit for the first time..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -q --timeout 60 --retries 10 -e .
fi
command -v git >/dev/null || { echo "Git was not found. Install git and run this again."; exit 1; }
echo "Starting DepExit... your browser will open. Keep this window open."
exec .venv/bin/depexit web
