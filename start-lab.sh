#!/usr/bin/env bash
# start-lab.sh — command-line launcher (Linux / Kali, macOS, WSL).
#
# This is the one place the launcher logic lives. "start-lab.desktop" calls this
# script, so a fix here fixes every way of starting the lab.
#
# Usage:
#   ./start-lab.sh              # serve + open the browser
#   ./start-lab.sh 9000         # prefer a specific port
#   ./start-lab.sh --no-open    # serve only, do not launch a browser

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PORT=""
OPEN=""
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN="--no-open" ;;
    --*) ;;
    [0-9]*) PORT="$arg" ;;
  esac
done

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 is required to run the lab (https://www.python.org/downloads/)." >&2
  exit 1
fi

if [ "$OPEN" = "--no-open" ]; then
  exec "$PY" serve.py ${PORT:+"$PORT"}
else
  exec "$PY" serve.py --open ${PORT:+"$PORT"}
fi