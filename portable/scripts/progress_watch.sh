#!/bin/sh
# Progress viewer: reprint the progress table every 2 seconds.
# Usage: ./progress_watch.sh [run_dir_or_project_root]
#   Without an argument: BILI_PROGRESS_DIR is used when set, otherwise walk
#   up from the current directory to the nearest folder holding .workbuddy.
#   Ctrl-C exits.
HERE=$(cd "$(dirname "$0")" && pwd)
HUB="$HERE/progress_hub.py"

if [ -n "$1" ]; then
  RUN="$1"
  if [ -d "$RUN/.workbuddy" ]; then
    RUN="$RUN/.workbuddy/cache/progress/current"
  fi
elif [ -n "$BILI_PROGRESS_DIR" ]; then
  RUN="$BILI_PROGRESS_DIR"
else
  ROOT=$(pwd)
  while [ ! -d "$ROOT/.workbuddy" ]; do
    if [ "$ROOT" = "/" ] || [ -z "$ROOT" ]; then
      ROOT=""
      break
    fi
    ROOT=$(dirname "$ROOT")
  done
  if [ -n "$ROOT" ]; then
    RUN="$ROOT/.workbuddy/cache/progress/current"
  fi
fi

if [ -z "$RUN" ] || [ ! -d "$RUN" ]; then
  echo "[ERROR] progress directory not found: $RUN"
  echo "Pass the project folder as the first argument, or set BILI_PROGRESS_DIR."
  exit 1
fi

if [ -n "$BILI_PYTHON" ]; then
  PY="$BILI_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[ERROR] Python not found. Set BILI_PYTHON or install Python 3.10+."
  exit 1
fi

if [ ! -f "$HUB" ]; then
  echo "[ERROR] progress_hub.py was not found next to this launcher: $HUB"
  exit 1
fi

while true; do
  clear
  "$PY" "$HUB" --status --dir "$RUN"
  sleep 2
done
