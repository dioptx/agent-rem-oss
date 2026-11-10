"""Render the morning briefing as a markdown checklist.

Three stacked layers per item — Event, Pattern, Proposals. Hidden HTML-comment markers (<!--P:id-->, <!--p:id-->)
let feedback.py map the human's checkmarks back to patterns and proposals;
they render invisibly in every markdown viewer.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
from .actions import recur_note
from .db import ConsolidationDB

MARKER = {"drag": "▼ DRAG", "lift": "▲ LIFT"}

LEGEND = (
    "> Check a box to **accept** · `snooze Nd` defers · `note:` comments (kept "
    "open) · `drop` dismisses this instance · `mute` silences the pattern for "
    "good · **leave blank and it carries over**, climbing until you act.\n>\n"
    "> Score weights recent and concrete signals highest — yesterday counts ×3, "
    "and a tool error or refusal outweighs a keyword correction.\n>\n"
    "> I don't act on these — a future agent does. Run `rem review` to "
    "log your marks, or `rem actions --preview` to see these insights as "
    "concrete, paste-ready actions (with the project/session they came from)."
)


def _inline(value) -> str:
    """Untrusted prose must not introduce journal control lines or marker comments."""
    return " ".join(str(value).splitlines()).replace("<!--", "&lt;!--")


def _local_hhmm(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M")
    except ValueError:
        return "—"


def _since_review_line(db: ConsolidationDB, run_date: str, pats: list) -> str:
    last = db.last_review_date()
    if not last or last >= run_date:
        return ""
    days = (date.fromisoformat(run_date) - date.fromisoformat(last)).days
    streaks = [(p.get("surfaced_streak") or 0) for p in pats if p]
    carried = sum(1 for s in streaks if s >= 1)
    escalated = sum(1 for s in streaks if s >= 2)
    new = sum(1 for s in streaks if s == 0)
    return (f"_{days}d since last review · {carried} carried over "
            f"({escalated} escalated) · {new} new · {db.faded_count()} faded_")


def render(db: ConsolidationDB, run_date: str) -> str:
    briefs = db.briefs_for(run_date)
    run = db.conn.execute(
        "SELECT * FROM runs WHERE run_date=? ORDER BY id DESC LIMIT 1", (run_date,)
    ).fetchone()
    window = run["window_days"] if run and run["window_days"] is not None else config.DEFAULT_WINDOW
    ran = _local_hhmm(run["started_at"]) if run else "—"
    sessions = run["sessions_scanned"] if run else 0
    nproj = len(json.loads(run["projects"])) if run and run["projects"] else 0

    pats = [db.get_pattern(b["pattern_id"]) for b in briefs]
    ndrag = sum(1 for p in pats if p and p["valence"] == "drag")
    nlift = sum(1 for p in pats if p and p["valence"] == "lift")

    lines = [
        f"# Night consolidation — {run_date} · ran {ran} · "
        f"{sessions} sessions / {nproj} projects",
        f"{len(briefs)} patterns · {ndrag} drag · {nlift} lift · "
        f"window {window}d",
    ]
    since = _since_review_line(db, run_date, pats)
    if since:
        lines.append(since)
    lines += ["", LEGEND, "", "---", ""]

    if not briefs:
        if sessions:
            lines.append("_Nothing recurred above the bar in the observed sessions._")
        else:
            lines.append("_No sessions were observed. Check the configured transcript folders, then run REM after a coding session._")
    for b in briefs:
        p = db.get_pattern(b["pattern_id"])
        if not p:
            continue
        marker = MARKER.get(p["valence"], p["valence"] or "")
        streak = p.get("surfaced_streak") or 0
        badge = f" `[Day {streak + 1} · unresolved]`" if streak >= 1 else ""
        if p.get("prior_decision"):     # accepted/dropped and it came back
            badge += f" `[{recur_note(p)}]`"
        lines.append(
            f"## {b['position']} {marker} · {p['axis']} · {_inline(p['title'])} · "
            f"{p['occurrence_count']} event groups (score {p['weighted_score']}){badge} "
            f"<!--P:{p['id']}-->"
        )
        lines.append(f"**Event** — {_inline(b['event_line'])}")
        lines.append(f"**Pattern** — {_inline(b['pattern_line'])}")
        snapshot = json.loads(b.get("evidence_json") or "null")
        if snapshot:
            lines.append(f"**Evidence** — {snapshot['window_start']} to {snapshot['window_end']}; "
                         f"{snapshot['event_groups']} event groups across {snapshot['sessions']} known sessions.")
            for event in snapshot["events"]:
                lines.append(f"> `{_inline(event['id'])}` · {_inline(event['occurred_date'])} · "
                             f"{_inline(event.get('agent') or 'unknown agent')} · session `{_inline(event.get('session_id') or 'unknown')}`")
                for quote in event["quotes"]:
                    lines.extend("> " + line for line in str(quote).splitlines())
        else:
            lines.append("**Evidence** — no source snapshot was recorded for this briefing.")
        lines.append("**Proposals**")
        props = db.proposals_for(p["id"], run_date)
        if not props:
            lines.append("- [ ] _(no proposal generated)_")
        for pr in props:
            lines.append(f"- [ ] T{pr['tier']} — {_inline(pr['text'])} <!--p:{pr['id']}-->")
        lines.append("  note:")
        lines.append("")
        lines.append("---")
        lines.append("")
    from .retro import defect_lines
    lines.extend(defect_lines(db))
    return "\n".join(lines).rstrip() + "\n"


def write_report(db: ConsolidationDB, run_date: str,
                 journal_dir: Path | None = None) -> Path:
    journal_dir = journal_dir or config.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    out = journal_dir / f"journal-{run_date}.md"
    out.write_text(render(db, run_date), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the morning briefing.")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--db", default=None)
    ap.add_argument("--journal-dir", default=None)
    args = ap.parse_args()
    db = ConsolidationDB(Path(args.db) if args.db else None)
    out = write_report(db, args.today,
                       Path(args.journal_dir) if args.journal_dir else None)
    n = len(db.briefs_for(args.today))
    print(f"wrote {out} ({n} items)")
    db.close()


if __name__ == "__main__":
    main()
