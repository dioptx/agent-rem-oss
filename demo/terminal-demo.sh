#!/usr/bin/env bash
# A short capture of the real CLI; no typing automation or model is required.
set -euo pipefail
demo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source_dir=${REM_SOURCE:-"$demo_dir/.."}
demo_data=$(mktemp -d "${TMPDIR:-/tmp}/rem-terminal.XXXXXX")
trap 'rm -rf -- "$demo_data"' EXIT
export PYTHONPATH="$source_dir${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export CONSOLIDATE_DB="$demo_data/events.db"
export CONSOLIDATE_JOURNAL_DIR="$demo_data/journals"
export REM_SETTINGS="$demo_data/settings.json"
python3 "$demo_dir/seed_demo.py" "$demo_data" >/dev/null
type_command() {
  python3 -c 'import sys,time
for char in sys.argv[1]:
    print(char, end="", flush=True)
    time.sleep(.035)
print()' "$1"
}
printf 'Synthetic demo data; no real sessions.\n\n'
type_command '$ rem ledger --runs 3'
bash "$source_dir/bin/rem" ledger --runs 3
sleep 2
brief_date=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["latest_journal"])' "$demo_data/demo.json")
type_command "\$ rem actions --preview --today $brief_date"
bash "$source_dir/bin/rem" actions --preview --today "$brief_date" | sed -n '1,38p'
sleep 2
