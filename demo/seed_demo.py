#!/usr/bin/env python3
"""Invent a REM ledger from constants and a fixed PRNG; never import user history."""

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import random
from unittest.mock import patch


SEED = 84721
NIGHTS = 56
PROJECTS = ("acme-widgets", "paper-kite")
PATTERNS = (
    ("Guessed a file path", "List the relevant folder before selecting a file."),
    ("Edited generated files", "Check the generated-file header before editing."),
    ("Retried a broken build", "Read the failing command before retrying it."),
    ("Skipped a boundary check", "Add a boundary case before changing the parser."),
    ("Reformatted unrelated files", "Keep formatting changes within the requested files."),
    ("Repeated a long explanation", "Give the result first, then the evidence."),
    ("Asked to repeat requirements", "Read the task notes before asking for the same detail."),
    ("Added a focused regression test", "Keep the small regression case with its fix."),
)


def seed_demo(folder, today=None):
    """Seed an empty directory. Same seed and date produce identical rows and files."""
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError("Choose a new, empty demo directory; existing data is never opened.")
    today = date.fromisoformat(today) if isinstance(today, str) else today or date.today()
    start = today - timedelta(days=NIGHTS - 1)
    rng = random.Random(SEED)
    clock = [start.isoformat() + "T12:00:00+00:00"]
    # Set the repo's settings override before its first import, without changing a home.
    os.environ.setdefault("REM_SETTINGS", str(folder / "settings.json"))
    from rem import db as db_module, report
    from rem.db import ConsolidationDB

    (folder / "settings.json").write_text("{}\n")
    journals = folder / "journals"
    journals.mkdir()
    with patch.object(db_module, "_now", side_effect=lambda: clock[0]):
        db = ConsolidationDB(folder / "events.db")
        # Status-history triggers use SQLite's clock; keep those rows deterministic too.
        db.conn.create_function("strftime", 2, lambda _format, _when: clock[0])
        try:
            ids = [None] * len(PATTERNS)
            proposals = {}
            for offset in range(NIGHTS):
                day = start + timedelta(days=offset)
                stamp = day.isoformat()
                clock[0] = stamp + "T12:00:00+00:00"
                run = db.start_run(stamp)
                sessions = []
                for agent in ("claude", "codex"):
                    for _ in range(4):
                        sid = f"demo-{rng.getrandbits(96):024x}"
                        db.upsert_session(agent, sid, stamp, headless=False)
                        sessions.append((agent, sid, rng.choice(PROJECTS)))

                # A failed final extraction leaves the last good journal intact.
                if offset == NIGHTS - 1:
                    db.finish_run(run, finished_at=stamp + "T12:03:00+00:00", status="failed",
                                  sessions_scanned=0, window_days=1,
                                  provenance_json=json.dumps({"demo": True, "failure": "invented model timeout"}))
                    continue

                active = [
                    offset < 28 and rng.random() < .8,
                    6 <= offset < 35 and rng.random() < .7,
                    offset in (5, 14, 23) or (offset >= 30 and offset % 2 == 0),
                    rng.random() < .4, rng.random() < .35,
                    offset < 8 and rng.random() < .8,
                    offset >= 44 and rng.random() < .8,
                    rng.random() < .25,
                ]
                events = 0
                for index, happens in enumerate(active):
                    if not happens:
                        continue
                    agent, sid, project = rng.choice(sessions)
                    valence = "lift" if index == 7 else "drag"
                    if ids[index] is None:
                        ids[index] = db.create_pattern(f"demo-{index}", "friction", valence,
                                                       PATTERNS[index][0], stamp)
                    if index == 2 and offset == 30:
                        db.reopen_with_context(ids[index], stamp, "accepted")
                    db.insert_event({
                        "id": f"demo-{rng.getrandbits(128):032x}", "run_date": stamp,
                        "occurred_date": stamp, "occurred_at": stamp + "T10:00:00+00:00",
                        "agent": agent, "session_id": sid, "project": project,
                        "git_branch": "demo-sandbox", "kind": "learning" if index == 7 else "error",
                        "valence": valence, "axis": "friction", "signature": f"demo-{index}",
                        "title": PATTERNS[index][0], "detail": "Invented example: " + PATTERNS[index][0],
                        "evidence": [f"Synthetic note from {project}: {PATTERNS[index][1]}"],
                        "pattern_id": ids[index], "signal_count": 1,
                    })
                    events += 1

                for index, decision_day in ((0, 28), (1, 35), (2, 28)):
                    if offset == decision_day:
                        pid = ids[index]
                        proposals[pid] = db.add_proposal(pid, 1, PATTERNS[index][1], stamp)
                        db.record_verdict(pid, stamp, "accept", proposals[pid],
                                          "Synthetic person: try this change.", created_at=clock[0])
                        db.set_pattern_status(pid, "accepted")
                        db.mark_resolved(pid, stamp)
                        if index != 2:
                            # A fictitious reference is display text, never a real host to query.
                            db.set_fix_pr(pid, f"demo:accepted-change-{index}")

                pending = [i for i in (6, 3, 2, 4, 7, 5)
                           if ids[i] is not None and db.get_pattern(ids[i])["status"] == "open"
                           and (i != 5 or offset < 8)]
                for position, index in enumerate(pending, 1):
                    pid = ids[index]
                    db.add_proposal(pid, 1, PATTERNS[index][1], stamp)
                    db.add_proposal(pid, 2, "Review a small synthetic example before widening the change.", stamp)
                    db.add_brief(stamp, pid, position,
                                 "Synthetic observation: " + PATTERNS[index][0],
                                 "This invented behaviour recurs in the demo projects.",
                                 recommend_tier=1, recommend_why="Start with the smallest change.")
                    db.bump_streak(pid)
                db.mark_surfaced([ids[i] for i in pending], stamp)
                db.finish_run(run, finished_at=stamp + "T12:03:00+00:00", status="ok",
                              sessions_scanned=len(sessions), events_extracted=events,
                              patterns_surfaced=len(pending), projects=json.dumps(PROJECTS), window_days=1,
                              provenance_json=json.dumps({"demo": True, "seed": SEED}))

            clock[0] = today.isoformat() + "T12:05:00+00:00"
            db.recompute_stats(today.isoformat())
            latest = (today - timedelta(days=1)).isoformat()
            report.write_report(db, latest, journals)
        finally:
            db.close()
    manifest = {"synthetic": True, "seed": SEED, "today": today.isoformat(),
                "nights": NIGHTS, "projects": list(PROJECTS),
                "quiet_patterns": ids[:2], "returned_pattern": ids[2],
                "untreated_patterns": ids[3:7], "latest_journal": latest}
    (folder / "demo.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # The demo context explicitly uses this empty heartbeat, not a person's job history.
    (folder / "history.jsonl").write_text("")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="new, empty output directory")
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="date anchor; defaults to the current date")
    args = parser.parse_args()
    seed_demo(args.folder, args.today)
    print("Seeded synthetic REM data; no session history was read.")


if __name__ == "__main__":
    main()
