"""Parse a briefing's marks into verdicts; carry over what wasn't acted on.

Verdict precedence per pattern: a checked proposal -> accept; an explicit `drop`
-> dismiss; `snooze Nd` -> defer; a non-empty `note:` -> comment (kept open);
nothing touched -> CARRY (re-surfaces, and its streak climbs). Blank never
drops — so skipping a day accumulates instead of nuking the list. Blockquote
lines (the legend) are ignored, so its instructional words never count.

`review` applies a journal on demand without bumping streaks (you're acting on
it now). The nightly run consolidates each *missed* prior journal exactly once
(processed_journals guard), bumping the streak of whatever you left untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import config, link
from .db import ConsolidationDB

HEADER_RE = re.compile(r"^##\s+.*<!--P:(\d+)-->\s*$")
PROP_RE = re.compile(r"^- \[( |x|X)\]\s.*<!--p:(\d+)-->")
NOTE_RE = re.compile(r"^\s*note:\s*(.*\S)?\s*$")
SNOOZE_RE = re.compile(r"^\s*(?:note:\s*)?snooze\s+(\d+)\s*d\b", re.IGNORECASE)
DROP_RE = re.compile(r"^\s*(drop|dismiss|reject)\b", re.IGNORECASE)
MUTE_RE = re.compile(r"^\s*(mute|silence)\b", re.IGNORECASE)
FNAME_RE = re.compile(r"journal-(\d{4}-\d{2}-\d{2})\.md$")


@dataclass
class PatternVerdict:
    pattern_id: int
    accepted_proposals: list[int] = field(default_factory=list)
    note: str = ""
    snooze_days: int | None = None
    explicit_drop: bool = False
    explicit_mute: bool = False

    @property
    def decision(self) -> str:
        if self.accepted_proposals:
            return "accept"
        if self.explicit_mute:      # kill the whole signature, never resurface
            return "mute"
        if self.explicit_drop:      # dismiss this instance; may recur w/ context
            return "drop"
        if self.snooze_days is not None:
            return "snooze"
        if self.note:
            return "comment"
        return "carry"


def parse_journal(text: str) -> list[PatternVerdict]:
    verdicts: dict[int, PatternVerdict] = {}
    current: PatternVerdict | None = None
    fence = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence or line.startswith(("    ", "\t")):
            continue
        if stripped.startswith(">"):
            continue  # legend / blockquote — never a verdict
        h = HEADER_RE.search(line)
        if h:
            current = PatternVerdict(pattern_id=int(h.group(1)))
            verdicts[current.pattern_id] = current
            continue
        if current is None:
            continue
        if PROP_RE.match(line):
            m = PROP_RE.match(line)
            if m.group(1).lower() == "x":
                current.accepted_proposals.append(int(m.group(2)))
            continue
        if SNOOZE_RE.search(line):
            current.snooze_days = int(SNOOZE_RE.search(line).group(1))
        n = NOTE_RE.match(line)
        if n and n.group(1):
            text_after = n.group(1).strip()
            if MUTE_RE.match(text_after):
                current.explicit_mute = True
            elif DROP_RE.match(text_after):
                current.explicit_drop = True
            else:
                current.note = text_after
        elif MUTE_RE.match(line):
            current.explicit_mute = True
        elif DROP_RE.match(line):
            current.explicit_drop = True
    return list(verdicts.values())


def apply(db: ConsolidationDB, verdicts: list[PatternVerdict], run_date: str,
          today: str, bump_carries: bool = True) -> dict:
    with db.transaction():
        bump_carries = bump_carries and not db.journal_processed(run_date)
        counts = {"accept": 0, "drop": 0, "mute": 0, "snooze": 0, "comment": 0, "carry": 0}
        times = db.verdict_times(run_date)
        for v in verdicts:
            dec = v.decision
            counts[dec] += 1
            revision = {"decision": dec}
            if dec == "accept":
                revision["accepted_proposals"] = sorted(set(v.accepted_proposals))
            elif dec == "snooze":
                revision["snooze_days"] = v.snooze_days
            fingerprint = hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()
            prior = db.conn.execute(
                "SELECT fingerprint FROM journal_pattern_revisions WHERE run_date=? AND pattern_id=?",
                (run_date, v.pattern_id)).fetchone()
            if prior and prior[0] == fingerprint:
                # Replaying an old decision must not close a recurrence or extend
                # a snooze, even when another item in the journal was edited.
                db.conn.execute(
                    "UPDATE verdicts SET comment=? WHERE run_date=? AND pattern_id=?",
                    (v.note, run_date, v.pattern_id))
                if bump_carries and dec == "carry":
                    db.bump_streak(v.pattern_id)
                continue
            db.conn.execute("DELETE FROM verdicts WHERE run_date=? AND pattern_id=?",
                            (run_date, v.pattern_id))
            db.conn.execute(
                "INSERT INTO journal_pattern_revisions (run_date, pattern_id, fingerprint) VALUES (?,?,?) "
                "ON CONFLICT(run_date, pattern_id) DO UPDATE SET fingerprint=excluded.fingerprint",
                (run_date, v.pattern_id, fingerprint))
            first = times.get((v.pattern_id, None, dec))
            if dec == "accept":
                for prop_id in v.accepted_proposals:
                    db.record_verdict(v.pattern_id, run_date, "accept",
                                      proposal_id=prop_id, comment=v.note,
                                      created_at=times.get((v.pattern_id, prop_id, "accept")))
                db.set_pattern_status(v.pattern_id, "accepted")
                db.mark_resolved(v.pattern_id, today)
            elif dec == "mute":
                db.record_verdict(v.pattern_id, run_date, "mute", comment=v.note, created_at=first)
                db.set_pattern_status(v.pattern_id, "muted")
                db.mark_resolved(v.pattern_id, today)
            elif dec == "drop":
                db.record_verdict(v.pattern_id, run_date, "drop", created_at=first)
                db.set_pattern_status(v.pattern_id, "dropped")
                db.mark_resolved(v.pattern_id, today)
            elif dec == "snooze":
                until = (date.fromisoformat(today) + timedelta(days=v.snooze_days)).isoformat()
                db.record_verdict(v.pattern_id, run_date, "snooze", comment=v.note, created_at=first)
                db.set_pattern_status(v.pattern_id, "snoozed", snoozed_until=until)
                db.mark_resolved(v.pattern_id, today)
            elif dec == "comment":
                db.record_verdict(v.pattern_id, run_date, "comment", comment=v.note, created_at=first)
                db.mark_resolved(v.pattern_id, today)   # engaged: reset streak, keep open
            elif bump_carries:                              # carry: climbs, never drops
                db.bump_streak(v.pattern_id)
        if bump_carries:
            db.mark_journal_processed(run_date)
        return counts


def _run_date_of(path: Path) -> str | None:
    m = FNAME_RE.search(path.name)
    return m.group(1) if m else None


def apply_journal_file(db: ConsolidationDB, path: Path, today: str,
                       bump_carries: bool) -> tuple[str, dict]:
    rd = _run_date_of(path) or today
    return rd, apply(db, parse_journal(path.read_text(encoding="utf-8")), rd,
                     today, bump_carries)


def process_pending(db: ConsolidationDB, today: str,
                    journal_dir: Path | None = None) -> dict:
    """Consolidate every prior briefing not yet processed (each exactly once)."""
    journal_dir = journal_dir or config.JOURNAL_DIR
    total = {"accept": 0, "drop": 0, "mute": 0, "snooze": 0, "comment": 0,
             "carry": 0, "journals": 0}
    if not journal_dir.exists():
        return total
    for path in sorted(journal_dir.glob("journal-*.md")):
        rd = _run_date_of(path)
        if not rd or rd >= today:
            continue
        with db.transaction():
            if db.journal_processed(rd):
                continue
            _, res = apply_journal_file(db, path, today, bump_carries=True)
        for k, val in res.items():
            total[k] += val
        total["journals"] += 1
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Consolidate prior-briefing verdicts.")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--journal", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--journal-dir", default=None)
    args = ap.parse_args()
    db = ConsolidationDB(Path(args.db) if args.db else None)
    if args.journal:
        rd, res = apply_journal_file(db, Path(args.journal), args.today, True)
        print(f"applied {rd}: {res}")
    else:
        jd = Path(args.journal_dir) if args.journal_dir else None
        print(f"consolidated: {process_pending(db, args.today, jd)}")
        try:   # close the loop on accepted fixes; a git hiccup must never stop the night
            linked = link.link_fixes(db, args.today)
            print(f"linked fixes: {len(linked)}" + "".join(f"\n  #{x['pattern_id']} {x['url']}" for x in linked))
        except Exception as e:
            print(f"linking fixes skipped: {type(e).__name__}: {e}")
    db.close()


if __name__ == "__main__":
    main()
