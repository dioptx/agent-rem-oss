"""One-time backfills behind the rem serve analytics. Each is idempotent: re-running adds nothing.

  python3 -m rem.backfill history     # status history for patterns that have none
  python3 -m rem.backfill sessions    # one sessions row per surviving transcript
  python3 -m rem.backfill all
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from . import extract
from .db import ConsolidationDB

VERDICT_STATUS = {"accept": "accepted", "drop": "dropped", "mute": "muted", "snooze": "snoozed"}

def history(db: ConsolidationDB) -> int:
    """Seed pattern_history for patterns with no creation row (created before the triggers
    existed): open at first_seen, one row per status-changing verdict at its first decision
    date, and a closing row when the current status differs (e.g. a fix that recurred and
    reopened), unless a trigger has already logged a later change."""
    todo = [r["id"] for r in db.conn.execute(
        "SELECT id FROM patterns WHERE id NOT IN "
        "(SELECT pattern_id FROM pattern_history WHERE from_status IS NULL)")]
    added = 0
    for pid in todo:
        p = db.get_pattern(pid)
        first_live = db.conn.execute(
            "SELECT at, from_status, to_status FROM pattern_history WHERE pattern_id=? "
            "ORDER BY at, id LIMIT 1", (pid,)).fetchone()
        entries = [(p["first_seen"] or (p["created_at"] or "")[:10], None, "open", "backfill-created")]
        status = "open"
        for v in db.conn.execute(
                "SELECT decision, run_date, MIN(created_at) t FROM verdicts WHERE pattern_id=? "
                "GROUP BY decision, run_date ORDER BY MIN(COALESCE(created_at, run_date))", (pid,)):
            to = VERDICT_STATUS.get(v["decision"])
            when = v["t"] or v["run_date"]
            if not to or to == status:
                continue
            if first_live and (when >= first_live["at"]
                               or (status, to) == (first_live["from_status"], first_live["to_status"])):
                break
            entries.append((when[:10], status, to, "backfill-verdict"))
            status = to
        if first_live:
            if status != first_live["from_status"]:
                entries.append((first_live["at"][:10], status, first_live["from_status"],
                                "backfill-bridge"))
        elif p["status"] != status:
            closing = (p["last_verdict_at"] if p["status"] != "open" else p["last_seen"]) or entries[-1][0]
            at = max(entries[-1][0], closing[:10])
            entries.append((at, status, p["status"], "backfill-current"))
        for at, before, after, source in entries:
            db.conn.execute(
                "INSERT INTO pattern_history (pattern_id, at, from_status, to_status, source) "
                "VALUES (?,?,?,?,?)", (pid, at, before, after, source))
            added += 1
    db.conn.commit()
    return added


def sessions(db: ConsolidationDB, today: str | None = None) -> int:
    """Record every transcript still on disk. Claude Code prunes old ones, so this reaches back
    only as far as they survive; the nightly extract keeps adding from here on."""
    today = today or date.today().isoformat()
    before = db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    for path in extract.iter_session_files(3650, today):
        scan = extract.scan_file(path, today, today)
        if scan:
            db.upsert_session(scan["agent"], scan["session_id"], scan.get("started_date"),
                              scan.get("headless", False))
    return db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] - before


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time backfills for rem serve.")
    ap.add_argument("what", choices=["history", "sessions", "all"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()
    db = ConsolidationDB(Path(args.db) if args.db else None)
    if args.what in ("history", "all"):
        print(f"history: {history(db)} rows added")
    if args.what in ("sessions", "all"):
        print(f"sessions: {sessions(db, args.today)} sessions added")
    db.close()


if __name__ == "__main__":
    main()
