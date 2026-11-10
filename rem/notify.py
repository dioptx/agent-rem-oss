"""Surface the finished briefing: auto-open it and fire a macOS notification.

Both side effects are best-effort — a headless night run must never fail just
because the GUI session isn't reachable. The authoritative heartbeat is the
CRON_RESULT line the prompt emits as its final message; this prints one too so
the module is useful when run by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import date
from pathlib import Path


def open_file(path: Path) -> bool:
    try:
        subprocess.run(["open", str(path)], check=False, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def notify(title: str, message: str) -> bool:
    # AppleScript escaping: double quotes and backslashes.
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Open the briefing + notify.")
    ap.add_argument("--journal", required=True)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    path = Path(args.journal)
    n = args.count
    summary = (f"{n} pattern{'s' if n != 1 else ''} to clear"
               if n else "quiet night — nothing recurred")
    # CONSOLIDATE_NO_OPEN silences GUI side effects (used for dry-runs).
    silent = bool(os.environ.get("CONSOLIDATE_NO_OPEN"))
    if path.exists() and not args.no_open and not silent:
        open_file(path)
    if not silent:
        notify("Night consolidation ready", f"{args.today}: {summary}")
    print("CRON_RESULT " + json.dumps(
        {"items": n, "summary": summary, "draft_posted": False}))


if __name__ == "__main__":
    main()
