"""`rem review` — apply the latest briefing's marks now, and echo what
landed, so you can verify it parsed correctly before the next run.

Does NOT bump carry-over streaks (you're acting on today's briefing, not letting
it lapse). Re-runnable: edit the markdown, run again, see the updated counts.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from . import config
from .feedback import PatternVerdict, apply, parse_journal, _run_date_of


def latest_journal(journal_dir: Path) -> Path | None:
    if not journal_dir.exists():
        return None
    files = sorted(journal_dir.glob("journal-*.md"))
    return files[-1] if files else None


def _describe(v: PatternVerdict) -> str:
    """One-line gloss of how a verdict parsed, so a typo is visible on sight."""
    if v.decision == "accept":
        return f"accept ({len(v.accepted_proposals)} proposal(s))"
    if v.decision == "snooze":
        return f"snooze {v.snooze_days}d"
    if v.decision == "comment":
        return f"note: {v.note[:50]}"
    return v.decision   # mute / drop / carry


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply + verify the latest briefing.")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--db", default=None)
    ap.add_argument("--journal-dir", default=None)
    ap.add_argument("--journal", default=None, help="a specific journal instead of latest")
    args = ap.parse_args()

    from .db import ConsolidationDB
    db = ConsolidationDB(Path(args.db) if args.db else None)
    jd = Path(args.journal_dir) if args.journal_dir else config.JOURNAL_DIR
    path = Path(args.journal) if args.journal else latest_journal(jd)
    if not path or not path.exists():
        print("no briefing found to review.")
        db.close()
        return

    rd = _run_date_of(path) or args.today
    verdicts = parse_journal(path.read_text(encoding="utf-8"))
    c = apply(db, verdicts, rd, args.today, bump_carries=False)

    print(f"reviewed {path.name}:")
    for v in verdicts:                       # per-item, so a misparse is obvious
        p = db.get_pattern(v.pattern_id)
        title = p["title"] if p else f"#{v.pattern_id}"
        print(f"  {_describe(v):24} · {title}")
    print(f"  ── accepted {c['accept']} · muted {c['mute']} · dropped {c['drop']} "
          f"· snoozed {c['snooze']} · noted {c['comment']} · carried {c['carry']}")
    if c["carry"]:
        print(f"  ({c['carry']} left open — they'll carry to the next briefing "
              f"and climb if they keep recurring)")
    db.close()


if __name__ == "__main__":
    main()
