"""Link accepted fixes to the commits that made them, so the loop closes without a hand-run
`rem actions --link`. A fix commit carries a trailer naming the thing it fixes:

    Rem-Fixes: 111

Every night (from rem.feedback) this scans the git repos your recent sessions worked in for that
trailer and records the commit on each accepted thing that has no fix yet.

    python3 -m rem.link [--window 14] [--today YYYY-MM-DD] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import date
from pathlib import Path

from . import config
from .db import ConsolidationDB
from .extract import iter_session_files

TRAILER_RE = re.compile(r"^Rem-Fixes:\s*([\d,\s]+)$", re.MULTILINE | re.IGNORECASE)
CWD_RE = re.compile(r'"cwd"\s*:\s*"([^"]+)"')
GITHUB_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")
HEAD_LINES = 40     # cwd shows up in a transcript's first records


def _git(root: Path | str, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20)
    return r.stdout if r.returncode == 0 else ""


def session_repos(window: int, today: str) -> set[Path]:
    """Git roots of every working directory a session in the window started in."""
    cwds: set[str] = set()
    for path in iter_session_files(window, today):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(HEAD_LINES), f):
                    m = CWD_RE.search(line)
                    if m:
                        cwds.add(m.group(1))
                        break
        except OSError:
            continue
    roots: set[Path] = set()
    for cwd in cwds:
        if Path(cwd).is_dir():
            top = _git(cwd, "rev-parse", "--show-toplevel").strip()
            if top:
                roots.add(Path(top))
    return roots


def commit_url(root: Path, sha: str) -> str:
    m = GITHUB_RE.search(_git(root, "remote", "get-url", "origin").strip())
    return f"https://github.com/{m.group(1)}/{m.group(2)}/commit/{sha}" if m else f"{root}@{sha[:12]}"


def find_fix_commits(root: Path, since_days: int) -> list[tuple[int, str]]:
    """(thing id, commit sha) for every Rem-Fixes trailer in commits on any branch since since_days."""
    out = _git(root, "log", "--all", f"--since={since_days}.days", "--format=%H%x00%B%x1e")
    found = []
    for rec in out.split("\x1e"):
        sha, _, body = rec.strip().partition("\x00")
        for m in TRAILER_RE.finditer(body):
            found += [(int(x), sha) for x in re.split(r"[,\s]+", m.group(1)) if x.isdigit()]
    return found


def link_fixes(db: ConsolidationDB, today: str, window: int = config.DEFAULT_WINDOW,
               roots: set[Path] | None = None, dry_run: bool = False) -> list[dict]:
    """Record the newest marked commit on each accepted thing that has no fix linked yet."""
    # an accepted thing that came back reopens as 'open', so go by the accept call, not the status
    accepted_open = {r["id"] for r in db.conn.execute(
        "SELECT DISTINCT p.id FROM patterns p JOIN verdicts v ON v.pattern_id = p.id "
        "WHERE v.decision = 'accept' AND p.fix_pr IS NULL "
        "AND COALESCE(p.status, 'open') NOT IN ('dropped', 'muted')")}
    linked: dict[int, dict] = {}
    for root in sorted(roots if roots is not None else session_repos(window, today)):
        for pid, sha in find_fix_commits(root, max(window, 30)):
            if pid in accepted_open and pid not in linked:     # git log is newest first
                linked[pid] = {"pattern_id": pid, "url": commit_url(root, sha), "repo": str(root)}
    if not dry_run:
        for item in linked.values():
            db.set_fix_pr(item["pattern_id"], item["url"])
    return list(linked.values())


def main() -> None:
    ap = argparse.ArgumentParser(description="Link accepted fixes to their Rem-Fixes commits.")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--window", type=int, default=config.DEFAULT_WINDOW)
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = ConsolidationDB(Path(args.db) if args.db else None)
    try:
        print(json.dumps(link_fixes(db, args.today, args.window, dry_run=args.dry_run), indent=1))
    finally:
        db.close()


if __name__ == "__main__":
    main()
