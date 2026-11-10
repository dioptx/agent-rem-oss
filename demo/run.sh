#!/usr/bin/env bash
set -euo pipefail
demo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source_dir=${REM_SOURCE:-"$demo_dir/.."}
if [ ! -f "$source_dir/rem/db.py" ]; then
  echo 'Set REM_SOURCE to a REM source checkout.' >&2
  exit 1
fi
demo_data=$(mktemp -d "${TMPDIR:-/tmp}/rem-demo.XXXXXX")
demo_pid=''
cleanup() {
  if [ -n "$demo_pid" ]; then
    kill "$demo_pid" 2>/dev/null || true
    wait "$demo_pid" 2>/dev/null || true
  fi
  rm -rf -- "$demo_data"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export PYTHONPATH="$source_dir${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export CONSOLIDATE_DB="$demo_data/events.db"
export CONSOLIDATE_JOURNAL_DIR="$demo_data/journals"
export REM_SETTINGS="$demo_data/settings.json"
python3 "$demo_dir/seed_demo.py" "$demo_data"
python3 "$demo_dir/serve_demo.py" "$demo_data" --port "${REM_SERVE_PORT:-3043}" &
demo_pid=$!
wait "$demo_pid"
