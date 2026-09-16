#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH PYTHONHOME || true

echo "HydroBridge"
echo "Working folder: $PWD"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Installing packages into .venv ..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c "import numpy, matplotlib, flask; print('Ready: numpy', numpy.__version__)"

echo
echo "Starting HydroBridge at http://127.0.0.1:5050"
echo "Leave this terminal open. Press Ctrl+C to stop."
echo
.venv/bin/python app.py
