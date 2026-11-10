"""Audit and archive explicitly reviewed duplicate and test records.

Run with --apply only after inspecting the dry-run report. Original rows remain
in event_archive; decisions, proposals and historical brief snapshots are kept.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
from .db import ConsolidationDB
from .extract import BENCH_DIR_RE, _event_id


def plan(conn, evidence: dict, projects: Path) -> dict:
    start, end = evidence["window"]["start"], evidence["window"]["end"]
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise ValueError("Review window is reversed")
    rows = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE occurred_date BETWEEN ? AND ?", (start, end))}
    result = {"window": {"start": start, "end": end}, "archive": [], "skipped": []}
    compare = ("agent", "session_id", "occurred_date", "kind", "evidence_json",
               "pattern_id", "signature", "axis", "valence", "project", "git_branch",
               "signal_count")
    retired = set()
    for ids in evidence["exact_duplicate_clusters"]:
        if len(ids) != 2 or len(set(ids)) != 2:
            raise ValueError("Expected distinct duplicate pairs")
        pair = [rows.get(i) for i in ids]
        if not all(pair):
            result["skipped"].append({"ids": ids, "reason": "missing or already archived"})
            continue
        a, b = pair
        if not a["agent"] or not a["session_id"] or any(a[k] != b[k] for k in compare):
            result["skipped"].append({"ids": ids, "reason": "identity or classification differs"})
            continue
        canonical = _event_id(a["agent"], a["session_id"], a["occurred_date"], a["kind"])
        if canonical not in ids or any(i in retired for i in ids):
            result["skipped"].append({"ids": ids, "reason": "no unambiguous current identity"})
            continue
        old = next(i for i in ids if i != canonical)
        result["archive"].append({"event_id": old, "canonical_id": canonical,
                                  "reason": "duplicate", "source": canonical})
        retired.add(old)

    # Only source-directory provenance establishes a test here. Pasted wording
    # and a classifier's category are not sufficient evidence to discard work.
    test_records = evidence.get("test_candidates", [])
    for expected in test_records:
        row = rows.get(expected["id"])
        if row is None or row["id"] in retired:
            continue
        if row["agent"] != "claude" or not row["session_id"]:
            result["skipped"].append({"ids": [row["id"]], "reason": "no Claude source identity"})
            continue
        if any(row[k] != expected[k] for k in ("agent", "session_id", "occurred_date")) \
                or json.loads(row["evidence_json"]) != expected["evidence"]:
            raise ValueError(f"Reviewed evidence changed: {row['id']}")
        session_id = row["session_id"]
        if Path(session_id).name != session_id or any(c in session_id for c in "*?[]"):
            raise ValueError("Invalid session identity")
        sources = list(projects.glob(f"*/{session_id}.jsonl"))
        if len(sources) != 1 or not BENCH_DIR_RE.search(sources[0].parent.name):
            result["skipped"].append({"ids": [row["id"]], "reason": "source does not prove test origin"})
            continue
        result["archive"].append({"event_id": row["id"], "canonical_id": None,
                                  "reason": "confirmed test directory", "source": str(sources[0])})
        retired.add(row["id"])
    # Do not archive a keeper selected above, even if it is also a test candidate.
    keepers = {x["canonical_id"] for x in result["archive"] if x["canonical_id"]}
    if keepers & retired:
        raise ValueError("Overlapping duplicate and test cleanup; audit separately")
    return result


def apply(db: ConsolidationDB, evidence: dict, projects: Path) -> dict:
    with db.transaction():
        result = plan(db.conn, evidence, projects)
        for item in result["archive"]:
            row = dict(db.conn.execute("SELECT * FROM events WHERE id=?", (item["event_id"],)).fetchone())
            db.conn.execute(
                "INSERT INTO event_archive VALUES (?,?,?,?,?,?)",
                (item["event_id"], item["canonical_id"], item["reason"], item["source"],
                 json.dumps(row), datetime.now(timezone.utc).isoformat()))
            db.conn.execute("DELETE FROM events WHERE id=?", (item["event_id"],))
        # Recompute using the latest recorded scoring window, not the review's
        # historical span. Statuses and human decisions remain untouched.
        latest = db.conn.execute(
            "SELECT run_date, window_days FROM runs WHERE status IN ('ok', 'extracted') "
            "AND window_days > 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if result["archive"] and latest and latest["window_days"]:
            db.recompute_stats(latest["run_date"], latest["window_days"])
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--projects", type=Path, default=config.CLAUDE_PROJECTS_DIR)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text())
    if not args.db.is_file() or not args.projects.is_dir():
        parser.error("Database and transcript directory must exist")
    if args.apply:
        db = ConsolidationDB(args.db)
        try:
            result = apply(db, evidence, args.projects)
        finally:
            db.close()
    else:
        conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            result = plan(conn, evidence, args.projects)
        finally:
            conn.close()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
