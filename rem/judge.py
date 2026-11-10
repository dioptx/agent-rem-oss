"""Deterministic I/O around the one LLM judgment step.

The night prompt drives four subcommands; all reasoning happens in the LLM
between them, never in this file:

  dump       -> JSON: today's unjudged candidates + open and recently decided patterns
  cluster    <- JSON: per-event axis/valence/signature + pattern assignment
  targets    -> JSON: the surfaced patterns (recurrence math is deterministic)
  save-brief <- JSON: Event/Pattern framing + 1-3 layered proposals per pattern

Clustering (which events are "the same recurring thing") is LLM-judged — cheap at ~5-15 events/day, no embedding infra.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import config
from .db import ConsolidationDB

# How far back the judge is shown decided patterns, so a fix that did not stick reattaches.
DECIDED_LOOKBACK_DAYS = 90


def _load(args) -> ConsolidationDB:
    return ConsolidationDB(Path(args.db) if args.db else None)


def cmd_dump(db: ConsolidationDB, today: str) -> dict:
    candidates = []
    for e in db.candidates():
        candidates.append({
            "id": e["id"], "kind": e["kind"], "agent": e.get("agent"),
            "project": e["project"],
            "occurred_date": e["occurred_date"], "title": e["title"],
            "detail": e["detail"],
            "source": e.get("source"), "plan_slug": e.get("plan_slug"),
            "phase": e.get("phase"), "valence": e.get("valence"),
            "evidence": json.loads(e["evidence_json"] or "[]"),
        })
    open_pats = [{
        "id": p["id"], "signature": p["signature"], "title": p["title"],
        "axis": p["axis"], "valence": p["valence"], "status": p["status"],
    } for p in db.open_patterns(today)]
    since = (date.fromisoformat(today) - timedelta(days=DECIDED_LOOKBACK_DAYS)).isoformat()
    decided = [{
        "id": p["id"], "signature": p["signature"], "title": p["title"],
        "axis": p["axis"], "valence": p["valence"],
        "status": p["status"], "decided": p["last_verdict_at"],
    } for p in db.decided_patterns(since)]
    return {"run_date": today, "candidates": candidates, "open_patterns": open_pats,
            "decided_patterns": decided}


def cmd_cluster(db: ConsolidationDB, today: str, payload: dict) -> dict:
    """payload: {"events":[{id, axis, valence, signature,
                 existing_pattern_id|pattern_key}], "patterns":{key:{...}}}"""
    with db.transaction():
        key_to_id: dict[str, int] = {}
        new_defs = payload.get("patterns", {})
        created = 0
        assigned = 0
        for ev in payload.get("events", []):
            original = db.conn.execute('SELECT kind FROM events WHERE id=?', (ev['id'],)).fetchone()
            if original and original['kind'] == 'defect':
                continue
            db.tag_event(ev["id"], ev["axis"], ev["valence"], ev["signature"])
            pid = ev.get("existing_pattern_id")
            existing = db.get_pattern(int(pid)) if pid is not None else None
            if existing:
                status = existing["status"]
                if status in ("accepted", "dropped"):
                    # An event dated on or before the decision predates the fix.
                    occurred = db.conn.execute(
                        "SELECT occurred_date FROM events WHERE id=?", (ev["id"],)).fetchone()
                    decided_on = (existing["last_verdict_at"] or "")[:10]
                    if not occurred or occurred[0] > decided_on:
                        # a decided pattern came back: reopen it with its verdict attached
                        db.reopen_with_context(int(pid), today, status)
                elif status != "muted":
                    # a recurring (possibly snooze-expired) pattern fired again
                    db.reactivate_pattern(int(pid), today)
            else:
                key = ev.get("pattern_key") or ev["signature"]
                if key not in key_to_id:
                    d = new_defs.get(key, {})
                    sig = d.get("signature", key)
                    # The dump only offered open and recently decided patterns, so
                    # the LLM mints a key for anything muted/accepted/dropped older
                    # than that. Reattach by signature so those verdicts persist
                    # across nightly re-clustering.
                    existing = db.find_pattern_by_signature(sig)
                    if existing:
                        key_to_id[key] = existing["id"]
                        # muted → stays muted (won't surface); open/snoozed → reuse
                    else:
                        key_to_id[key] = db.create_pattern(
                            sig, d.get("axis", ev["axis"]),
                            d.get("valence", ev["valence"]), d.get("title", key), today)
                        created += 1
                pid = key_to_id[key]
                existing = db.get_pattern(pid)
                if existing["status"] in ("accepted", "dropped"):
                    occurred = db.conn.execute(
                        "SELECT occurred_date FROM events WHERE id=?", (ev["id"],)).fetchone()
                    decided_on = (existing["last_verdict_at"] or "")[:10]
                    if not occurred or occurred[0] > decided_on:
                        db.reopen_with_context(pid, today, existing["status"])
            db.assign_event(ev["id"], int(pid))
            assigned += 1
        return {"assigned": assigned, "patterns_created": created}


def cmd_targets(db: ConsolidationDB, today: str, window: int) -> list[dict]:
    db.recompute_stats(today, window)
    out = []
    start = (date.fromisoformat(today) - timedelta(days=window)).isoformat()
    for p in db.surface():
        evs = [e for e in db.events_for_pattern(p["id"])
               if start <= e["occurred_date"] <= today and e["signature"] is not None]
        evidence: list[str] = []
        projects = sorted({e["project"] for e in evs if e["project"]})
        for e in evs:
            evidence.extend(json.loads(e["evidence_json"] or "[]"))
        out.append({
            "pattern_id": p["id"], "signature": p["signature"],
            "title": p["title"], "axis": p["axis"], "valence": p["valence"],
            "occurrence_count": p["occurrence_count"],
            "weighted_score": p["weighted_score"], "projects": projects,
            "agents": sorted({e["agent"] for e in evs if e.get("agent")}),
            "window_start": start, "window_end": today,
            "event_groups": len(evs),
            "sessions": len({(e.get("agent") or "unknown", e["session_id"])
                             for e in evs if e.get("session_id")}),
            "unknown_session_groups": sum(not e.get("session_id") for e in evs),
            "evidence_events": [{"id": e["id"], "occurred_date": e["occurred_date"],
                                 "agent": e.get("agent"), "session_id": e.get("session_id"),
                                 "detail": e["detail"],
                                 "quotes": json.loads(e["evidence_json"] or "[]")}
                                for e in evs[:6]],
            "evidence": evidence[:6],
            "details": [e["detail"] for e in evs][:6],
        })
    return out


def cmd_save_brief(db: ConsolidationDB, payload: dict, evidence_targets=None) -> dict:
    with db.transaction():
        run_date = payload["run_date"]
        if evidence_targets is None:
            run = db.conn.execute("SELECT window_days FROM runs WHERE run_date=? ORDER BY id DESC LIMIT 1",
                                  (run_date,)).fetchone()
            window = run[0] if run and run[0] is not None else config.DEFAULT_WINDOW
            evidence_targets = cmd_targets(db, run_date, window)
        evidence_by_pattern = {t["pattern_id"]: t for t in evidence_targets}
        db.clear_briefs(run_date)
        surfaced: list[int] = []
        for pos, item in enumerate(payload.get("items", []), start=1):
            pid = int(item["pattern_id"])
            rec = item.get("recommend") or {}
            tier = rec.get("tier")
            db.add_brief(run_date, pid, pos, item.get("event", ""), item.get("pattern", ""),
                         int(tier) if isinstance(tier, int) or str(tier).isdigit() else None,
                         str(rec.get("why", "")))
            target = evidence_by_pattern.get(pid)
            snapshot = ({"window_start": target["window_start"], "window_end": target["window_end"],
                         "sessions": target["sessions"], "event_groups": target["event_groups"],
                         "events": target["evidence_events"]} if target else None)
            db.conn.execute("UPDATE briefs SET evidence_json=? WHERE run_date=? AND pattern_id=?",
                            (json.dumps(snapshot) if snapshot else None, run_date, pid))
            db.clear_proposals(pid, run_date)
            for tier, text in enumerate(item.get("proposals", []), start=1):
                db.add_proposal(pid, tier, text, run_date)
            surfaced.append(pid)
        db.mark_surfaced(surfaced, run_date)   # for streak/escalation bookkeeping
        db.set_patterns_surfaced(run_date, len(surfaced))  # ledger run timeline
        return {"items": len(surfaced)}


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM-judgment I/O for consolidation.")
    ap.add_argument("command", choices=["dump", "cluster", "targets", "save-brief"])
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--window", type=int, default=config.DEFAULT_WINDOW)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    db = _load(args)

    if args.command == "dump":
        print(json.dumps(cmd_dump(db, args.today), indent=2))
    elif args.command == "cluster":
        payload = json.load(sys.stdin)
        print(json.dumps(cmd_cluster(db, args.today, payload)))
    elif args.command == "targets":
        print(json.dumps(cmd_targets(db, args.today, args.window), indent=2))
    elif args.command == "save-brief":
        payload = json.load(sys.stdin)
        print(json.dumps(cmd_save_brief(db, payload)))
    db.close()


if __name__ == "__main__":
    main()
