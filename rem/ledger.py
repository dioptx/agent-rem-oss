"""`rem ledger` — the ongoing record across all runs, read-only.

Every run, event, pattern, and verdict already persists in SQLite (append-only),
and each briefing is archived as dated markdown/HTML. This is the one-command
view of that accumulation — the run timeline, the patterns still carrying, and
the decision log — so "do we keep track?" has a visible answer, and an empty
decision log is impossible to miss.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import config
from .db import ConsolidationDB


def render(db: ConsolidationDB, runs_shown: int = 8) -> str:
    def q(sql, *a):
        return db.conn.execute(sql, a).fetchall()

    meta = dict(q("SELECT COUNT(*) n, MIN(run_date) lo, MAX(run_date) hi FROM runs")[0])
    out = [f"# Consolidation ledger — {meta['n']} runs, "
           f"{meta['lo'] or '—'} → {meta['hi'] or '—'}", ""]

    out.append("Runs (most recent):")
    for r in q("SELECT run_date, sessions_scanned s, patterns_surfaced p "
               "FROM runs ORDER BY run_date DESC LIMIT ?", runs_shown):
        out.append(f"  {r['run_date']}  {r['s']:>3} sessions · surfaced {r['p']}")

    out.append("")
    out.append("Standing patterns (open, longest-carrying):")
    rows = q("SELECT title, valence, occurrence_count oc, surfaced_streak st, "
             "prior_decision pd FROM patterns WHERE status='open' AND surfaced_streak>=1 "
             "ORDER BY surfaced_streak DESC, weighted_score DESC LIMIT 10")
    if rows:
        for r in rows:
            tag = f"  (recurred after {r['pd']})" if r["pd"] else ""
            out.append(f"  Day {r['st'] + 1:>2}  ×{r['oc']:<3} {r['valence']:4} "
                       f"{r['title']}{tag}")
    else:
        out.append("  (none carrying)")

    out.append("")
    dec = {r["decision"]: r["n"] for r in
           q("SELECT decision, COUNT(*) n FROM verdicts GROUP BY decision")}
    total = sum(dec.values())
    if dec:
        out.append(f"Decisions logged: {total}  (" +
                   " · ".join(f"{k} {v}" for k, v in dec.items()) + ")")
    else:
        out.append("Decisions logged: 0")
    if total == 0 and meta["n"] > 2:
        out.append(f"  ⚠ {meta['n']} runs, 0 verdicts — nothing marked yet; every "
                   f"pattern just carries and climbs. The loop was never closed.")

    out.append(f"Faded (stopped recurring): {db.faded_count()}")
    size = db.db_path.stat().st_size // 1024 if db.db_path.exists() else 0
    njournals = (len(list(config.JOURNAL_DIR.glob("journal-*.md")))
                 if config.JOURNAL_DIR.exists() else 0)
    out.append(f"Store: {db.db_path.name} ({size}K) · "
               f"{njournals} archived journals")
    out.extend(['', 'Plan retrospectives (latest items):'])
    for row in q("SELECT project, plan_slug, phase, kind, valence, detail FROM events WHERE source='retro' "
                 "ORDER BY occurred_date DESC, id LIMIT 30"):
        out.append(f"  {row['project']}/{row['plan_slug']} · {row['phase']} · {row['kind']}/{row['valence']}: {row['detail']}")
    for row in q('SELECT metadata_json FROM retro_files ORDER BY ingested_on DESC, path LIMIT 30'):
        import json
        metadata = json.loads(row['metadata_json'])
        for change in metadata.get('changes', []):
            out.append(f"  {metadata['project']}/{metadata['plan']} · {change['phase']} · {change['evidence']}")
    from .retro import defect_lines
    out.extend(['', *defect_lines(db)])
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Show the ongoing consolidation ledger.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--runs", type=int, default=8)
    args = ap.parse_args()
    db = ConsolidationDB(Path(args.db) if args.db else None)
    print(render(db, args.runs))
    db.close()


if __name__ == "__main__":
    main()
