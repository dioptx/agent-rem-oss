"""Read-only metrics for the rem serve page.

Every function takes a connection from connect() and returns plain dicts and lists ready for
JSON. The connection is query_only, so nothing here can write.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config

HEARTBEAT = Path.home() / ".local" / "share" / "cron-jobs" / "consolidation" / "history.jsonl"
BEFORE_DAYS = 28          # historical context before acceptance
MIN_COVERED_DAYS = 5      # a week is readable only if REM covered this many of its days
THIN_WEEK_SESSIONS = 20   # fewer sessions than this (or a quarter of a usual week) = too few to compare
# frozen: the default look-back before runs.window_days existed. Those runs
# reached back at least this far, some further (see run_windows).
LEGACY_WINDOW = 5
DECIDED = ("accepted", "dropped", "muted")
QUIET_DAYS = 30          # an open thing with no repeat in this many days has gone quiet
DECISION_OF = {"accepted": "accept", "dropped": "drop", "muted": "mute"}
# A shift takes 2-5 minutes and the job kills it at 40, so a run row still open after this is stuck.
STUCK_MINUTES = 60
MATCH_SLACK_S = 60       # a run row belongs to the shift whose job window it started in, give or take
YES_DAYS = 30            # how far back "fixes you said yes to" reaches


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    uri = Path(db_path or config.DB_PATH).resolve().as_uri() + "?mode=ro"   # never creates a DB
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")   # the nightly run may hold a write lock briefly
    conn.execute("PRAGMA query_only=ON")
    return conn


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _local_day(ts: str) -> date:
    """A stored day or timestamp as a local calendar day. Timestamps are written in UTC
    (triggers and db._now); one without a zone is read as UTC."""
    if len(ts) == 10:
        return date.fromisoformat(ts)
    t = _utc(ts)
    if t is None:
        raise ValueError(f"not a timestamp: {ts!r}")
    return t.astimezone().date()


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _utc(ts) -> datetime | None:
    """A stored timestamp as an aware UTC datetime; None when it doesn't parse."""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _verdict_day(r) -> date:
    """The local day a verdict was made: its created_at, falling back to its run_date."""
    return _local_day(r["created_at"]) if r["created_at"] else _d(r["run_date"])


def first_decisions(conn, decision: str | None = None) -> dict[int, dict]:
    """Each pattern's first accept/drop/mute: {pattern_id: {"date", "decision"}}.
    The date is the verdict's first created_at (local day), falling back to its run_date. A
    pattern with no such verdict (accepted with no proposal ticked writes none) takes its first
    move to accepted/dropped/muted in pattern_history."""
    out: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT pattern_id, decision, run_date, created_at FROM verdicts "
            "WHERE decision IN ('accept','drop','mute') ORDER BY COALESCE(created_at, run_date)"):
        if decision is None or r["decision"] == decision:
            out.setdefault(r["pattern_id"], {"date": _verdict_day(r).isoformat(), "decision": r["decision"]})
    for r in conn.execute(
            "SELECT pattern_id, at, to_status FROM pattern_history "
            "WHERE to_status IN ('accepted','dropped','muted') ORDER BY at, id"):
        if decision is None or DECISION_OF[r["to_status"]] == decision:
            out.setdefault(r["pattern_id"], {"date": _local_day(r["at"]).isoformat(),
                                          "decision": DECISION_OF[r["to_status"]]})
    return out


def routing_start(conn) -> date | None:
    """The first nightly run on the judge that links a decided friction's recurrences back to
    its pattern (the same change started recording runs.window_days). Before it, a recurrence
    became a new pattern, so a decided pattern's own events stop at the decision."""
    d = conn.execute("SELECT MIN(run_date) FROM runs WHERE window_days IS NOT NULL "
                     "AND finished_at IS NOT NULL AND status IN ('ok','extracted')").fetchone()[0]
    return _d(d) if d else None


def _covered_in(covered: set[date], start: date, end: date) -> int:
    """Covered days in [start, end]."""
    return sum(1 for i in range((end - start).days + 1) if start + timedelta(days=i) in covered)


def fix_stuck(conn, today: str) -> list[dict]:
    """Linked event groups around acceptance, without judging intervention effectiveness.

    A completed extraction window is scan coverage, not proof of complete transcripts or
    comparable activity. A change link is not proof of implementation or activation.
    """
    t = _d(today)
    covered = covered_days(conn)
    routed = routing_start(conn)
    out = []
    for pid, first in first_decisions(conn, "accept").items():
        p = conn.execute("SELECT title, status, fix_pr FROM patterns WHERE id=?", (pid,)).fetchone()
        if p is None:
            continue
        when = _d(first["date"])
        b0 = when - timedelta(days=BEFORE_DAYS)
        # before routing starts nothing after a decision is measurable: an empty after window
        a0 = max(when + timedelta(days=1), routed) if routed else t + timedelta(days=1)
        seen = [_d(r[0]) for r in conn.execute(
            "SELECT occurred_date FROM events WHERE pattern_id=? AND signature IS NOT NULL", (pid,))]
        before = sum(1 for x in seen if b0 <= x < when)
        after = sum(1 for x in seen if a0 <= x <= t)
        observed_before = _covered_in(covered, b0, when - timedelta(days=1))
        observed_after = _covered_in(covered, a0, t)
        after_days = (t - when).days
        label = ("linked evidence observed" if after else
                 "no linked evidence observed" if observed_after else "coverage unavailable")
        last_observed = max((x for x in seen if a0 <= x <= t), default=None)
        out.append({"pattern_id": pid, "title": p["title"], "status": p["status"],
                    "fix_pr": p["fix_pr"], "decided": first["date"], "label": label,
                    "window_start": a0.isoformat() if routed and a0 <= t else None,
                    "window_end": today,
                    "last_observed": last_observed.isoformat() if last_observed else None,
                    "unit": "event groups",
                    "before": before, "after": after, "after_days": after_days,
                    "observed_before": observed_before, "observed_after": observed_after})
    out.sort(key=lambda r: (r["decided"], r["pattern_id"]), reverse=True)
    return out


def headline(fixes: list[dict]) -> str:
    if not fixes:
        return "No accepted proposals to follow yet."
    observed = sum(f["after"] > 0 for f in fixes)
    return (f"{observed} of {len(fixes)} accepted patterns have linked evidence after acceptance. "
            "This does not establish whether a change helped.")


def run_windows(conn) -> list[tuple[date, int]]:
    """Each run's (run_date, look-back days). A run from before window_days was recorded
    reached back at least LEGACY_WINDOW days, and as far as the oldest event it first inserted
    (source refreshes preserve the run_date of the run that first found each event)."""
    out = []
    for r in conn.execute(
            "SELECT run_date, window_days, (SELECT MIN(occurred_date) FROM events e "
            "WHERE e.run_date = runs.run_date) AS oldest FROM runs "
            "WHERE finished_at IS NOT NULL AND status IN ('ok','extracted','backfill')"):
        end = _d(r["run_date"])
        if r["window_days"] is not None:
            out.append((end, r["window_days"]))
        else:
            reach = (end - _d(r["oldest"])).days if r["oldest"] else 0
            out.append((end, max(LEGACY_WINDOW, reach)))
    return out


def covered_days(conn) -> set[date]:
    """Every day some nightly run's window reached."""
    days: set[date] = set()
    for end, window in run_windows(conn):
        days.update(end - timedelta(days=i) for i in range(window + 1))
    return days


def friction_weekly(conn, today: str) -> list[dict]:
    """Per week and agent: non-headless sessions started, how many of those hit friction, and
    the rate. `gap` names why a week can't be read: REM not running, REM not yet tracking that
    agent (before the week of its first event), events still waiting for the night shift to
    judge them, or transcripts pruned. `snag_sessions` counts
    interactive sessions by the week of their first snag; it needs no session total, so it
    stays readable (`count_gap` None) in weeks whose transcripts were pruned, and in weeks
    REM covered only partly (`partial`, with `covered` of `days` nights)."""
    t = _d(today)
    covered = covered_days(conn)
    if not covered:
        return []
    earliest = {r[0]: _monday(_d(r[1])) for r in conn.execute(
        "SELECT agent, MIN(started_date) FROM sessions WHERE started_date IS NOT NULL GROUP BY agent")}
    tracked = {r[0]: _monday(_d(r[1])) for r in conn.execute(
        "SELECT COALESCE(agent, 'claude'), MIN(occurred_date) FROM events GROUP BY 1")}
    friction = {(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT COALESCE(agent, 'claude'), session_id FROM events WHERE valence='drag'")}
    headless = {(r[0], r[1]) for r in conn.execute(
        "SELECT agent, session_id FROM sessions WHERE headless=1")}
    unjudged = {(_monday(_d(r[1])), r[0]) for r in conn.execute(
        "SELECT DISTINCT COALESCE(agent, 'claude'), occurred_date FROM events WHERE valence IS NULL")}
    snag_counts: dict[tuple, int] = {}            # interactive sessions by week of their first snag
    for agent, sid, first in conn.execute(
            "SELECT COALESCE(agent, 'claude'), session_id, MIN(occurred_date) FROM events "
            "WHERE valence='drag' GROUP BY 1, 2"):
        if (agent, sid) not in headless:
            key = (_monday(_d(first)), agent)
            snag_counts[key] = snag_counts.get(key, 0) + 1
    counts: dict[tuple, list[int]] = {}
    for s in conn.execute("SELECT agent, session_id, started_date FROM sessions "
                          "WHERE headless=0 AND started_date IS NOT NULL"):
        row = counts.setdefault((_monday(_d(s["started_date"])), s["agent"]), [0, 0])
        row[0] += 1
        row[1] += (s["agent"], s["session_id"]) in friction
    out = []
    week = _monday(min(covered))
    while week <= t:
        span = [week + timedelta(days=i) for i in range(7) if week + timedelta(days=i) <= t]
        cov = sum(1 for x in span if x in covered)
        for agent in ("claude", "codex"):
            n, f = counts.get((week, agent), [0, 0])
            watch_gap = ("not tracked yet" if agent not in tracked or week < tracked[agent]
                         else "not judged yet" if (week, agent) in unjudged else None)
            if cov < min(MIN_COVERED_DAYS, len(span)):
                gap = "REM not running"
            elif watch_gap:
                gap = watch_gap
            elif agent not in earliest or week < earliest[agent]:
                gap = "transcripts pruned"
            else:
                gap = None
            # A count is a floor, so a partly covered week still says something (a share doesn't),
            # unless the floor is 0.
            partial = cov < min(MIN_COVERED_DAYS, len(span))
            snags = snag_counts.get((week, agent), 0)
            count_gap = ("REM not running" if cov == 0 else watch_gap
                         or ("REM not running" if partial and not snags else None))
            out.append({"week": week.isoformat(), "agent": agent, "sessions": n,
                        "with_friction": f,
                        "rate": round(f / n, 3) if n and not gap else None, "gap": gap,
                        "snag_sessions": snags,
                        "count_gap": count_gap, "covered": cov, "days": len(span),
                        "partial": partial})
        week += timedelta(days=7)
    return out


def backlog(conn, today: str) -> dict:
    """Open vs decided patterns at each week's end (replayed from pattern_history), how many of
    the open ones had gone quiet by then (no event in QUIET_DAYS), patterns in the newest briefing
    still waiting on the user, and days from first seen to a decision."""
    t = _d(today)
    hist = conn.execute(
        "SELECT pattern_id, at, to_status FROM pattern_history ORDER BY at, id").fetchall()
    seen: dict[int, list[date]] = {}
    for pid, day in conn.execute("SELECT pattern_id, occurred_date FROM events "
                                 "WHERE pattern_id IS NOT NULL ORDER BY occurred_date"):
        seen.setdefault(pid, []).append(_d(day))

    def quiet(pid: int, end: date) -> bool:
        days = seen.get(pid, [])
        k = bisect.bisect_right(days, end)
        return k > 0 and (end - days[k - 1]).days > QUIET_DAYS

    weeks = []
    if hist:
        status: dict[int, str] = {}
        i, week = 0, _monday(_local_day(hist[0]["at"]))
        while week <= t:
            end = min(week + timedelta(days=6), t)
            while i < len(hist) and _local_day(hist[i]["at"]) <= end:
                status[hist[i]["pattern_id"]] = hist[i]["to_status"]
                i += 1
            open_ids = [p for p, s in status.items() if s in ("open", "snoozed")]
            weeks.append({"week": week.isoformat(), "open": len(open_ids),
                          "quiet": sum(1 for p in open_ids if quiet(p, end)),
                          "decided": sum(1 for s in status.values() if s in DECIDED)})
            week += timedelta(days=7)
    gaps = []
    for pid, first in first_decisions(conn).items():
        row = conn.execute("SELECT first_seen FROM patterns WHERE id=?", (pid,)).fetchone()
        if row and row[0]:
            gaps.append((_d(first["date"]) - _d(row[0])).days)
    return {"weeks": weeks, "waiting": waiting(conn),
            "days_to_decision": ({"median": statistics.median(gaps), "max": max(gaps),
                                  "n": len(gaps)} if gaps else None)}


def waiting(conn) -> int:
    """Patterns in the newest briefing with no verdict for it that are still open. An accept
    with no proposal ticked writes no verdict row but does move the pattern to accepted."""
    return conn.execute(
        "SELECT COUNT(DISTINCT b.pattern_id) FROM briefs b JOIN patterns p ON p.id = b.pattern_id "
        "WHERE b.run_date = (SELECT MAX(run_date) FROM briefs) AND p.status = 'open' "
        "AND NOT EXISTS (SELECT 1 FROM verdicts v "
        "                WHERE v.pattern_id = b.pattern_id AND v.run_date = b.run_date)"
    ).fetchone()[0]


def run_health(path: Path = HEARTBEAT, conn=None, n: int = 30,
               now: datetime | None = None) -> list[dict]:
    """The last n night shifts, oldest first: one row per cron heartbeat (the file repeats some).
    With the DB, exit 0 is not taken on trust: a shift whose run rows were all left open ended
    without finishing, so it 'got stuck'. The newest run row, while no heartbeat has closed it,
    is added as the shift in progress: 'running' for STUCK_MINUTES, then 'stuck'."""
    path = Path(path)
    seen, rows = set(), []
    for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = r.get("ts_utc")
        if not ts or ts in seen:
            continue
        seen.add(ts)
        code = r.get("exit")
        rows.append({"ts": ts, "exit": code, "duration_s": r.get("duration_s"),
                     "items": r.get("items"), "summary": r.get("summary") or "",
                     "status": "ok" if code == 0 else "timed out" if code == 124 else "failed"})
    rows = rows[-n:]
    if conn is None:
        return rows
    portable = []
    for run in conn.execute("SELECT * FROM runs WHERE provenance_json IS NOT NULL "
                            "AND provenance_json != '{}' ORDER BY started_at, id"):
        start, finish = _utc(run["started_at"]), _utc(run["finished_at"])
        if start is None:
            continue
        elapsed = max(0, ((finish or now or datetime.now(timezone.utc)) - start).total_seconds())
        status = run["status"]
        if finish is None:
            status = "running" if elapsed <= STUCK_MINUTES * 60 else "stuck"
        elif status != "ok":
            status = "failed"
        portable.append({"ts": (finish or start).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "exit": 0 if status == "ok" else None if status == "running" else 1,
                         "duration_s": round(elapsed), "items": run["patterns_surfaced"],
                         "summary": "Portable nightly run", "status": status,
                         "sessions": run["sessions_scanned"], "open": finish is None,
                         "source": "coordinator", "start": start})
    # Coordinator records supersede heartbeat guesses from the point of adoption onward.
    cutoff = portable[0]["start"] if portable else None
    if cutoff:
        rows = [r for r in rows if _utc(r["ts"]) and _utc(r["ts"]) < cutoff]
    scanned = {r[0]: r[1] for r in conn.execute(
        "SELECT run_date, MAX(sessions_scanned) FROM runs GROUP BY run_date")}
    db_runs = []
    for r in conn.execute("SELECT started_at, finished_at, status, sessions_scanned FROM runs "
                          "WHERE COALESCE(status, '') != 'backfill' AND COALESCE(provenance_json, '{}') = '{}'"):
        start = _utc(r["started_at"])
        if start:
            db_runs.append({"start": start, "open": r["finished_at"] is None,
                            "status": r["status"], "sessions": r["sessions_scanned"]})
    db_runs.sort(key=lambda x: x["start"])
    newest_end = None
    for r in rows:
        r["sessions"] = scanned.get(r["ts"][:10])
        end = _utc(r["ts"])
        if end and (newest_end is None or end > newest_end):
            newest_end = end
        if r["status"] != "ok" or end is None:
            continue
        begin = end - timedelta(seconds=(r["duration_s"] or 0) + MATCH_SLACK_S)
        mine = [x for x in db_runs if begin <= x["start"] <= end]
        if mine and all(x["open"] for x in mine):
            r["status"] = "stuck"
            r["sessions"] = max(x["sessions"] or 0 for x in mine)   # not a later run that day
    last = db_runs[-1] if db_runs else None
    if (last and last["open"] and last["status"] == "running"
            and not (newest_end and newest_end >= last["start"])):
        minutes = ((now or datetime.now(timezone.utc)) - last["start"]).total_seconds() / 60
        rows.append({"ts": last["start"].strftime("%Y-%m-%dT%H:%M:%SZ"), "exit": None,
                     "duration_s": max(0, round(minutes * 60)), "items": None, "summary": "",
                     "status": "running" if minutes <= STUCK_MINUTES else "stuck",
                     "sessions": last["sessions"], "open": True})
    for run in portable:
        run.pop("start")
    return sorted(rows + portable, key=lambda r: _utc(r["ts"]))[-n:]


def ended(runs: list[dict]) -> list[dict]:
    """Shifts that are over, stuck ones included: everything but one still on shift."""
    return [r for r in runs if r["status"] != "running"]


def shift_health(runs: list[dict], heartbeat: Path | None = None) -> dict:
    """The honest night-shift state for the top of the page. `missed` is every shift since the
    last one that slept fine, oldest first; `current` is a run still open with no heartbeat;
    `log` is the job's own newest note, read only when something went wrong."""
    missed, last_ok = [], None
    for r in reversed(ended(runs)):
        if r["status"] == "ok":
            last_ok = r["ts"]
            break
        missed.append(r)
    missed.reverse()
    state = ("none" if not runs else "running" if runs[-1]["status"] == "running"
             else "failing" if missed else "ok")
    return {"state": state, "missed": missed, "last_ok": last_ok,
            "current": runs[-1] if runs and runs[-1].get("open") else None,
            "log": last_run_note(heartbeat) if missed and heartbeat and runs[-1].get("source") != "coordinator" else None}


def last_run_note(heartbeat: Path = HEARTBEAT, limit: int = 400) -> dict | None:
    """The tail of what the cron wrapper last wrote (last-run.txt beside the heartbeat) and when.
    The wrapper rewrites it during a run, so it can be about a newer run than the last heartbeat."""
    path = Path(heartbeat).parent / "last-run.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
        at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    return {"text": text[-limit:], "at": at.strftime("%Y-%m-%dT%H:%M:%SZ")} if text else None


def accepted_fixes(conn, today: str, days: int = YES_DAYS) -> list[dict]:
    """Things you said yes to a fix idea for in the last `days` days, one row per thing: its newest
    yes, that day's fix ideas (empty when the idea was rewritten since), your note, and the linked
    edit (patterns.fix_pr) once there is one. Things you later dropped or hushed are left out.
    Unlinked first, then newest first."""
    t = _d(today)
    since = t - timedelta(days=days)
    out: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT v.pattern_id, v.run_date, v.created_at, v.comment, p.title, p.status, "
            "p.fix_pr, pr.tier, pr.text, pr.id AS proposal_id, "
            "im.effective_on, im.scope, im.reference, im.recorded_at AS implementation_recorded_at "
            "FROM verdicts v JOIN patterns p ON p.id = v.pattern_id "
            "LEFT JOIN proposals pr ON pr.id = v.proposal_id "
            "LEFT JOIN implementations im ON im.proposal_id = pr.id "
            "WHERE v.decision = 'accept' AND p.status NOT IN ('dropped', 'muted') ORDER BY v.id"):
        day = _verdict_day(r)
        if day < since:
            continue
        pid, decided = r["pattern_id"], day.isoformat()
        row = out.get(pid)
        if row is None or decided > row["decided"]:
            row = out[pid] = {"pattern_id": pid, "title": r["title"], "decided": decided,
                              "age_days": (t - day).days, "ideas": [], "implementations": [], "note": "",
                              "fix_pr": r["fix_pr"] or None,
                              "back": r["status"] in ("open", "snoozed")}
        elif decided < row["decided"]:
            continue
        if r["text"] and all(i["text"] != r["text"] for i in row["ideas"]):
            row["ideas"].append({"tier": r["tier"], "text": r["text"]})
        if r["effective_on"] and not any(i["proposal_id"] == r["proposal_id"] for i in row["implementations"]):
            row["implementations"].append({"proposal_id": r["proposal_id"], "proposal": r["text"],
                "effective_on": r["effective_on"], "scope": r["scope"], "reference": r["reference"],
                "recorded_at": r["implementation_recorded_at"], "source": "user-reported"})
        if (r["comment"] or "").strip() and not row["note"]:
            row["note"] = r["comment"].strip()
    for row in out.values():
        row["ideas"].sort(key=lambda i: i["tier"] or 0)
    return sorted(out.values(), key=lambda r: (r["fix_pr"] is not None, r["age_days"],
                                                r["pattern_id"]))


def changes(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT at, key, old, new, source FROM settings_log ORDER BY at")]


def _combined(weekly: list[dict]) -> list[dict]:
    """Both agents together, readable weeks only, oldest first."""
    by_week: dict[str, list[int]] = {}
    for r in weekly:
        if r["gap"]:
            continue
        row = by_week.setdefault(r["week"], [0, 0])
        row[0] += r["sessions"]
        row[1] += r["with_friction"]
    return [{"week": w, "sessions": n, "with_friction": f, "rate": f / n}
            for w, (n, f) in sorted(by_week.items()) if n]


def overview(conn, today: str, heartbeat: Path = HEARTBEAT, now: datetime | None = None) -> dict:
    """Everything the page needs in one payload."""
    fixes = fix_stuck(conn, today)
    weekly = friction_weekly(conn, today)
    combined = _combined(weekly)
    runs = run_health(heartbeat, conn, now=now)
    shift = shift_health(runs, heartbeat)
    bl = backlog(conn, today)
    t = _d(today)
    complete = [r for r in combined if _d(r["week"]) + timedelta(days=6) < t]
    now = complete[-1] if complete else None
    prev4 = prev4_sessions = None
    if now:
        w = _d(now["week"])
        prev = [r for r in combined if w - timedelta(days=28) <= _d(r["week"]) < w]
        n = sum(r["sessions"] for r in prev)
        prev4 = sum(r["with_friction"] for r in prev) / n if n else None
        prev4_sessions = round(n / len(prev)) if prev else None
    week_n = now["sessions"] if now else 0
    # a handful of long sessions can read as a big jump: too few to compare
    thin = bool(now) and (week_n < THIN_WEEK_SESSIONS
                          or (prev4_sessions is not None and week_n < prev4_sessions / 4))
    over = ended(runs)
    last14 = over[-14:]
    return {
        "today": today,
        "headline": headline(fixes),
        "tiles": {
            "fixes": {"accepted": len(fixes),
                      "with_evidence": sum(f["after"] > 0 for f in fixes),
                      "coverage_unavailable": sum(not f["observed_after"] for f in fixes)},
            "friction": {"week": now["week"] if now else None,
                         "rate": now["rate"] if now else None,
                         "sessions": week_n,
                         "with_friction": now["with_friction"] if now else 0,
                         "prev4": prev4, "prev4_sessions": prev4_sessions, "thin": thin},
            "waiting": {"count": bl["waiting"], "days_to_decision": bl["days_to_decision"]},
            "runs": {"ok": sum(1 for r in last14 if r["status"] == "ok"), "total": len(last14),
                     "missed": len(shift["missed"]),
                     "last": over[-1] if over else None},
        },
        "shift": shift,
        "accepted": accepted_fixes(conn, today),
        "fixes": fixes,
        "friction": weekly,
        "backlog": bl["weeks"],
        "runs": runs,
        "changes": changes(conn),
        "fix_dates": sorted({f["decided"] for f in fixes}),
    }
