"""Translate insights into concrete, paste-ready actions.

A proposal in the briefing is a *direction*; an action is that direction bound
to its evidence — the projects, sessions, branches and quotes the pattern was
extracted from — so it can be pasted straight into a working session (with a
human watching) instead of re-investigated from scratch. This is the
observe→act bridge, and like the rest of the agent it never executes anything.

    python3 -m rem.actions --preview   # tonight's insights, as actions
    python3 -m rem.actions             # the accepted-action queue
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from .db import ConsolidationDB


def record_implementation(db: ConsolidationDB, proposal_id: int, effective_on: str,
                          scope: str, reference: str) -> None:
    """Record a human report, not independently verified activation or efficacy."""
    effective = date.fromisoformat(effective_on)
    if effective > date.today() or not scope.strip() or not reference.strip():
        raise ValueError("Provide a past or current effective date, scope, and change reference")
    with db.transaction():
        if not db.conn.execute(
                "SELECT 1 FROM verdicts WHERE proposal_id=? AND decision='accept'",
                (proposal_id,)).fetchone():
            raise ValueError("Implementation must refer to an accepted proposal")
        db.conn.execute(
            "INSERT INTO implementations VALUES (?,?,?,?,?) "
            "ON CONFLICT(proposal_id) DO UPDATE SET effective_on=excluded.effective_on, "
            "scope=excluded.scope, reference=excluded.reference, recorded_at=excluded.recorded_at",
            (proposal_id, effective.isoformat(), scope.strip(), reference.strip(),
             datetime.now(timezone.utc).isoformat()))


def fix_state(url: str) -> str:
    """merged / open / closed for a PR url, or unknown when gh cannot say."""
    try:
        r = subprocess.run(["gh", "pr", "view", url, "--json", "state", "-q", ".state"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip().lower() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def recur_note(p: dict) -> str:
    """'recurred after you accepted it', plus the fix PR and its state when the act step opened one."""
    if not p.get("prior_decision"):
        return ""
    note = f"recurred after you {p['prior_decision']} it"
    if p.get("fix_pr"):
        note += f" (fix {p['fix_pr']}, {fix_state(p['fix_pr'])})"
    return note


def _pointers(evs: list[dict]) -> list[str]:
    """The *dominant* loci, by event count — a diffuse pattern hitting 16
    projects gets 'top-3 (+13 more)', not a useless dump of all of them."""
    def top(field: str, n: int, trim: int | None = None) -> str | None:
        vals = [(e[field][:trim] if trim else e[field])
                for e in evs if e.get(field)]
        if not vals:
            return None
        c = Counter(vals)
        head = [k for k, _ in c.most_common(n)]
        extra = len(c) - len(head)
        return ", ".join(head) + (f" (+{extra} more)" if extra > 0 else "")

    where = []
    projects = top("project", 3)
    if projects:
        where.append("projects " + projects)
    sessions = top("session_id", 2, trim=8)
    if sessions:
        where.append("sessions " + sessions)
    return where


def _quotes(evs: list[dict], limit: int = 3) -> list[str]:
    out: list[str] = []
    for e in evs:
        for q in json.loads(e.get("evidence_json") or "[]"):
            if q and q not in out:
                out.append(q)
    return out[:limit]


def _item(db: ConsolidationDB, pattern: dict, proposal: dict, when: str,
          note: str = "") -> dict:
    """One action as structured data — the direction bound to its evidence."""
    evs = db.events_for_pattern(pattern["id"])
    return {
        "pattern_id": pattern["id"], "proposal_id": proposal["id"], "recur": recur_note(pattern),
        "direction": proposal["text"], "tier": proposal["tier"],
        "title": pattern["title"], "axis": pattern["axis"],
        "valence": pattern["valence"], "occurrence_count": pattern["occurrence_count"],
        "when": when, "where": _pointers(evs), "note": note,
        "prior_decision": pattern.get("prior_decision"), "quotes": _quotes(evs),
    }


def render_action(item: dict) -> str:
    """A paste-ready action: the direction + where it came from + what proves it."""
    lines = [
        f"### {item['direction']}",
        f"_“{item['title']}” · {item['axis']}/{item['valence']} · "
        f"{item['occurrence_count']} recorded event groups · {item['when']}_",
    ]
    lines.append(f"Pattern: {item['pattern_id']}")
    if item.get("proposal_id") is not None:
        lines.append(f"Proposal: {item['proposal_id']}")
    if item["where"]:
        lines.append("Where: " + " · ".join(item["where"]))
    if item["recur"]:
        lines.append(f"> {item['recur']}; implementation effectiveness is not established")
    if item["note"]:
        lines.append(f"Note: {item['note']}")
    if item["quotes"]:
        lines.append("Evidence:")
        lines += [f"- {q}" for q in item["quotes"]]
    return "\n".join(lines)


def preview_items(db: ConsolidationDB, today: str) -> list[dict]:
    """Every surfaced insight translated to its layered actions — no accept needed."""
    out = []
    for b in db.briefs_for(today):
        p = db.get_pattern(b["pattern_id"])
        if not p:
            continue
        for pr in db.proposals_for(p["id"], today):
            out.append(_item(db, p, pr, "surfaced " + today))
    return out


def queue_items(db: ConsolidationDB, since: str | None) -> list[dict]:
    """The accepted-action queue: what you signed off on, ready to run."""
    out = []
    for v in db.accepted_verdicts(since):
        p = db.get_pattern(v["pattern_id"])
        if not p:
            continue
        props = {pr["id"]: pr for pr in db.proposals_for(p["id"], include_archived=True)}
        pr = props.get(v["proposal_id"])
        if pr:
            out.append(_item(db, p, pr, "accepted " + v["run_date"],
                             note=v.get("comment", "")))
    return out


def preview(db: ConsolidationDB, today: str) -> list[str]:
    return [render_action(i) for i in preview_items(db, today)]


def queue(db: ConsolidationDB, since: str | None) -> list[str]:
    return [render_action(i) for i in queue_items(db, since)]


def main() -> None:
    ap = argparse.ArgumentParser(description="Translate insights into paste-ready actions.")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--preview", action="store_true",
                    help="render every surfaced insight as an action (no accept needed)")
    ap.add_argument("--since", default=None, help="only accepted on/after this date")
    ap.add_argument("--db", default=None)
    ap.add_argument("--implemented", type=int, metavar="PROPOSAL_ID")
    ap.add_argument("--effective-on", help="user-reported implementation date, YYYY-MM-DD")
    ap.add_argument("--scope", help="where the change became effective")
    ap.add_argument("--reference", help="commit, PR, configuration, or other change reference")
    ap.add_argument("--link", nargs=2, metavar=("PATTERN_ID", "PR_URL"),
                    help="record the PR the act step opened for a pattern")
    args = ap.parse_args()
    if args.implemented is not None and (
            args.link or not all((args.effective_on, args.scope, args.reference))):
        ap.error("--implemented requires --effective-on, --scope and --reference; omit --link")
    if args.implemented is None and any((args.effective_on, args.scope, args.reference)):
        ap.error("implementation details require --implemented PROPOSAL_ID")
    db = ConsolidationDB(Path(args.db) if args.db else None)
    if args.implemented is not None:
        try:
            record_implementation(db, args.implemented, args.effective_on, args.scope, args.reference)
        except ValueError as exc:
            ap.error(str(exc))
        finally:
            db.close()
        print(f"Proposal {args.implemented}: user-reported implementation recorded")
        return
    if args.link:
        db.set_fix_pr(int(args.link[0]), args.link[1])
        print(f"pattern {args.link[0]} -> {args.link[1]}")
        db.close()
        return

    actions = preview(db, args.today) if args.preview else queue(db, args.since)
    if not actions:
        print("_No surfaced insights._" if args.preview else
              "_Nothing accepted yet. Check a box, then `rem review`._")
    else:
        head = "Tonight's insights, as actions" if args.preview else "Accepted-action queue"
        print(f"# {head} ({len(actions)})\n")
        print("\n\n".join(actions))
    db.close()


if __name__ == "__main__":
    main()
