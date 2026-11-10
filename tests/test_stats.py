"""Metrics on a fixture DB with known answers, including a fix that got worse."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from rem import stats
from rem.db import ConsolidationDB

TODAY = "2026-09-30"


@pytest.fixture
def conn(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")    # writable: fixtures write, stats code only reads
    yield db.conn
    db.close()


def add_pattern(c, pid, title, first_seen="2026-07-01", status="accepted", streak=0):
    c.execute("INSERT INTO patterns (id, signature, axis, valence, title, first_seen, last_seen, "
              "status, surfaced_streak) VALUES (?,?,?,?,?,?,?,?,?)",
              (pid, f"s{pid}", "friction", "drag", title, first_seen, first_seen, status, streak))


def add_events(c, pid, dates, session="x", agent="claude", valence="drag"):
    for i, d in enumerate(dates):
        c.execute("INSERT INTO events (id, run_date, occurred_date, session_id, agent, kind, "
                  "valence, pattern_id, signature) VALUES (?,?,?,?,?,?,?,?,?)",
                  (f"{pid}-{session}-{i}", d, d, f"{session}{i}", agent, "error", valence, pid, f"s{pid}"))


def decide(c, pid, day, decision="accept", proposal_id=None, comment=""):
    c.execute("INSERT INTO verdicts (proposal_id, pattern_id, run_date, decision, comment, created_at) "
              "VALUES (?,?,?,?,?,?)", (proposal_id, pid, day, decision, comment, f"{day}T10:00:00+00:00"))


def days(start, n, step=1):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i * step)).isoformat() for i in range(n)]


def add_runs(c, run_dates, window=1):
    """Nightly runs that finished, as the real job leaves them."""
    for d in run_dates:
        c.execute("INSERT INTO runs (run_date, started_at, finished_at, status, window_days) "
                  "VALUES (?,?,?,?,?)", (d, f"{d}T06:30:00Z", f"{d}T06:33:00Z", "extracted", window))


def add_brief(c, run_date, pid, position=1):
    c.execute("INSERT INTO briefs (run_date, pattern_id, position) VALUES (?,?,?)",
              (run_date, pid, position))


@pytest.fixture
def fixes(conn):
    add_runs(conn, days("2026-08-01", 61))                            # every day covered, routed
    add_pattern(conn, 1, "Quiet")
    add_events(conn, 1, days("2026-08-05", 8, 3) + ["2026-09-10"])   # 2.0/wk before, 0.24/wk after
    decide(conn, 1, "2026-09-01")
    add_pattern(conn, 2, "Worse")
    add_events(conn, 2, days("2026-08-10", 4, 5) + days("2026-09-05", 10, 2))  # 1.0 -> 2.41/wk
    decide(conn, 2, "2026-09-01")
    add_pattern(conn, 3, "Early")
    add_events(conn, 3, days("2026-09-01", 5, 3))
    decide(conn, 3, "2026-09-25")                                     # 5 days: too early
    add_pattern(conn, 4, "Thin")
    add_events(conn, 4, ["2026-08-20", "2026-08-25"])                 # 2 before: not enough
    decide(conn, 4, "2026-09-01")
    add_pattern(conn, 5, "Dropped", status="dropped")
    decide(conn, 5, "2026-09-01", "drop")                             # not a fix: excluded
    return conn


def test_connect_is_read_only_and_never_creates_a_db(tmp_path):
    missing = tmp_path / "typo.db"
    with pytest.raises(sqlite3.OperationalError):
        stats.connect(missing)
    assert not missing.exists()
    ConsolidationDB(tmp_path / "events.db").close()
    c = stats.connect(tmp_path / "events.db")
    with pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO runs (run_date, started_at) VALUES ('2026-09-30', 'x')")
    c.close()


def test_local_day_converts_utc_timestamps(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert stats._local_day("2026-09-30") == date(2026, 9, 30)
        assert stats._local_day("2026-09-30T03:00:00Z") == date(2026, 9, 29)
        assert stats._local_day("2026-09-30T03:00:00.123Z") == date(2026, 9, 29)
        assert stats._local_day("2026-09-30T03:00:00") == date(2026, 9, 29)       # no zone = UTC
        assert stats._local_day("2026-09-30T03:00:00+00:00") == date(2026, 9, 29)
        assert stats._local_day("2026-09-30T20:00:00-07:00") == date(2026, 9, 30)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_followup_reports_observations_without_grading(fixes):
    rows = {r["title"]: r for r in stats.fix_stuck(fixes, TODAY)}
    assert rows["Quiet"]["label"] == rows["Worse"]["label"] == "linked evidence observed"
    assert rows["Thin"]["label"] == "no linked evidence observed"
    assert rows["Early"]["label"] == "no linked evidence observed"
    worse = rows["Worse"]
    assert (worse["before"], worse["after"]) == (4, 10)
    assert (worse["observed_before"], worse["observed_after"]) == (28, 29)
    assert worse["last_observed"] == "2026-09-23"
    assert worse["unit"] == "event groups"


def test_fix_after_window_over_uncovered_days_is_not_measurable(conn):
    add_runs(conn, days("2026-08-01", 31))                            # August, routed
    add_runs(conn, days("2026-09-25", 6))                             # then a gap until 09-24
    add_pattern(conn, 1, "Outage")
    add_events(conn, 1, days("2026-08-05", 6, 4))
    decide(conn, 1, "2026-09-01")
    row, = stats.fix_stuck(conn, TODAY)
    assert (row["label"], row["observed_before"], row["observed_after"]) == \
        ("no linked evidence observed", 28, 7)


def test_fix_decided_before_routing_counts_only_after_routing(conn):
    for d in days("2026-08-01", 50):                                  # legacy runs, no window_days
        conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status) VALUES (?, 'x', 'x', 'ok')", (d,))
    add_runs(conn, days("2026-09-20", 11), window=5)                  # routing starts 09-20
    add_pattern(conn, 1, "Pre-routing")
    add_events(conn, 1, days("2026-08-05", 6, 4) + ["2026-09-05", "2026-09-25"])
    decide(conn, 1, "2026-09-01")
    row, = stats.fix_stuck(conn, TODAY)
    assert (row["label"], row["after"], row["observed_after"]) == ("linked evidence observed", 1, 11)
    add_runs(conn, days("2026-10-01", 10), window=5)
    row, = stats.fix_stuck(conn, "2026-10-10")
    assert (row["label"], row["after"], row["observed_after"]) == ("linked evidence observed", 1, 21)


def test_fix_with_no_routed_run_is_not_measurable(conn):
    for d in days("2026-08-01", 61):
        conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status) VALUES (?, 'x', 'x', 'ok')", (d,))
    add_pattern(conn, 1, "Old judge")
    add_events(conn, 1, days("2026-08-05", 6, 4))
    decide(conn, 1, "2026-09-01")
    assert stats.routing_start(conn) is None
    row, = stats.fix_stuck(conn, TODAY)
    assert (row["label"], row["observed_before"], row["observed_after"]) == \
        ("coverage unavailable", 28, 0)


def test_first_decisions_fall_back_to_status_history(conn):
    add_runs(conn, days("2026-08-01", 61))
    add_pattern(conn, 7, "Accepted with no proposal ticked", status="dropped")
    add_pattern(conn, 8, "Has a verdict", status="accepted")
    conn.execute("DELETE FROM pattern_history")
    for pid, at, before, after in ((7, "2026-09-01", None, "open"),
                                   (7, "2026-09-12T09:00:00Z", "open", "accepted"),
                                   (7, "2026-09-20T09:00:00Z", "accepted", "dropped"),
                                   (8, "2026-09-02T09:00:00Z", "open", "muted")):
        conn.execute("INSERT INTO pattern_history (pattern_id, at, from_status, to_status) "
                     "VALUES (?,?,?,?)", (pid, at, before, after))
    decide(conn, 8, "2026-09-05")
    got = stats.first_decisions(conn)
    assert got[7] == {"date": "2026-09-12", "decision": "accept"}
    assert got[8] == {"date": "2026-09-05", "decision": "accept"}      # a verdict wins
    assert {r["pattern_id"] for r in stats.fix_stuck(conn, TODAY)} == {7, 8}


def test_legacy_run_window_is_inferred_from_the_events_it_inserted(conn):
    conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status) VALUES ('2026-09-17', 'x', 'x', 'ok')")
    conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status) VALUES ('2026-08-10', 'x', 'x', 'ok')")
    conn.execute("INSERT INTO runs (run_date, started_at, window_days, finished_at, status) "
                 "VALUES ('2026-09-30', 'x', 1, 'x', 'ok')")
    for eid, run_date, occurred in (("a", "2026-09-17", "2026-09-03"),     # reached back 14 days
                                    ("b", "2026-09-17", "2026-09-15"),
                                    ("c", "2026-08-10", "2026-08-08"),     # 2 days: still 5
                                    ("d", "2026-09-30", "2026-09-20")):    # explicit window wins
        conn.execute("INSERT INTO events (id, run_date, occurred_date, kind) VALUES (?,?,?,?)",
                     (eid, run_date, occurred, "error"))
    assert sorted(stats.run_windows(conn)) == [(date(2026, 8, 10), 5), (date(2026, 9, 17), 14),
                                               (date(2026, 9, 30), 1)]
    covered = stats.covered_days(conn)
    assert {date.fromisoformat(d) for d in days("2026-09-03", 15)} <= covered
    assert date(2026, 9, 2) not in covered and date(2026, 8, 5) in covered


def test_headline_describes_history_not_effectiveness(fixes):
    assert stats.headline(stats.fix_stuck(fixes, TODAY)) == (
        "2 of 4 accepted patterns have linked evidence after acceptance. "
        "This does not establish whether a change helped.")
    assert stats.headline([]) == "No accepted proposals to follow yet."


def test_link_does_not_change_observation_label(conn):
    add_runs(conn, days("2026-09-01", 30))
    add_pattern(conn, 1, "Review me")
    decide(conn, 1, "2026-09-10")
    add_events(conn, 1, ["2026-09-09", "2026-09-10", "2026-09-11", "2026-10-01"])
    before, = stats.fix_stuck(conn, TODAY)
    conn.execute("UPDATE patterns SET fix_pr='https://example.test/branch/commit'")
    after, = stats.fix_stuck(conn, TODAY)
    assert before["label"] == after["label"] == "linked evidence observed"
    assert after["after"] == 1 and after["last_observed"] == "2026-09-11"
    assert after["window_start"] == "2026-09-11"
    assert "grade_from" not in after


def add_session(c, agent, sid, started, headless=0):
    c.execute("INSERT INTO sessions (agent, session_id, started_date, headless) VALUES (?,?,?,?)",
              (agent, sid, started, headless))


def add_event(c, eid, sid, agent, valence, day="2026-09-29"):
    c.execute("INSERT INTO events (id, run_date, occurred_date, session_id, agent, kind, valence) "
              "VALUES (?,?,?,?,?,?,?)", (eid, day, day, sid, agent, "error", valence))


def test_friction_rate_uses_the_same_sessions_top_and_bottom(conn):
    conn.execute("INSERT INTO runs (run_date, started_at, window_days, finished_at, status) VALUES ('2026-09-30','x',5,'x','ok')")
    for sid in ("c1", "c2", "c3", "c4"):
        add_session(conn, "claude", sid, "2026-09-28")
    add_session(conn, "claude", "h1", "2026-09-28", headless=1)
    add_session(conn, "codex", "x1", "2026-09-28")
    add_event(conn, "e1", "c1", "claude", "drag")
    add_event(conn, "e2", "c2", None, "drag")          # pre-agent-column rows are Claude
    add_event(conn, "e3", "c3", "claude", "lift")      # a win is not friction
    add_event(conn, "e4", "h1", "claude", "drag")      # headless sessions are excluded
    add_event(conn, "e5", "x1", "codex", "lift")       # codex is tracked, no friction
    rows = {(r["week"], r["agent"]): r for r in stats.friction_weekly(conn, TODAY)}
    claude = rows[("2026-09-28", "claude")]
    assert (claude["sessions"], claude["with_friction"], claude["rate"], claude["gap"]) == \
        (4, 2, 0.5, None)
    assert rows[("2026-09-28", "codex")]["rate"] == 0.0
    assert rows[("2026-09-21", "claude")]["gap"] == "REM not running"   # 3 of 7 days covered


def test_friction_weeks_before_an_agents_first_event_are_not_tracked(conn):
    add_runs(conn, days("2026-09-08", 23))
    add_session(conn, "claude", "c1", "2026-09-14")
    add_session(conn, "claude", "c2", "2026-09-22")
    add_session(conn, "codex", "x1", "2026-09-15")
    add_session(conn, "codex", "x2", "2026-09-22")
    add_event(conn, "e1", "c1", "claude", "drag", "2026-09-14")
    add_event(conn, "e2", "x2", "codex", "drag", "2026-09-23")
    rows = {(r["week"], r["agent"]): r for r in stats.friction_weekly(conn, TODAY)}
    assert rows[("2026-09-14", "codex")]["gap"] == "not tracked yet"   # over "transcripts pruned"
    assert rows[("2026-09-14", "codex")]["rate"] is None
    assert rows[("2026-09-21", "codex")]["rate"] == 1.0
    assert rows[("2026-09-14", "claude")]["rate"] == 1.0
    assert rows[("2026-09-07", "claude")]["gap"] == "not tracked yet"


def test_friction_tile_is_the_last_full_week_against_a_sessions_weighted_baseline(conn, tmp_path):
    add_runs(conn, days("2026-08-20", 18) + days("2026-09-13", 18))   # 09-07 week: REM down
    weeks = {"2026-08-17": (5, 5),     # 5 weeks back: outside the baseline
             "2026-08-24": (10, 5), "2026-08-31": (2, 0), "2026-09-07": (6, 6),
             "2026-09-14": (4, 1),
             "2026-09-21": (8, 2),     # the latest full week
             "2026-09-28": (3, 3)}     # this week, not over yet
    for day, (n, f) in weeks.items():
        for i in range(n):
            add_session(conn, "claude", f"{day}-{i}", day)
            if i < f:
                add_event(conn, f"{day}-e{i}", f"{day}-{i}", "claude", "drag", day)
    tile = stats.overview(conn, TODAY, tmp_path / "none.jsonl")["tiles"]["friction"]
    assert (tile["week"], tile["rate"], tile["sessions"]) == ("2026-09-21", 0.25, 8)
    assert tile["prev4"] == 6 / 16                     # (5+0+1)/(10+2+4); unweighted mean is 0.25
    assert tile["thin"] is True                        # 8 sessions: too few to compare


def test_friction_tile_is_thin_only_when_the_week_is_small(conn, tmp_path):
    add_runs(conn, days("2026-08-24", 35))
    weeks = {"2026-08-24": 40, "2026-08-31": 40, "2026-09-07": 40, "2026-09-14": 40,
             "2026-09-21": 30}                         # the latest full week
    for day, n in weeks.items():
        for i in range(n):
            add_session(conn, "claude", f"{day}-{i}", day)
        add_event(conn, f"{day}-e0", f"{day}-0", "claude", "drag", day)
    tile = stats.overview(conn, TODAY, tmp_path / "none.jsonl")["tiles"]["friction"]
    assert (tile["thin"], tile["prev4_sessions"]) == (False, 40)
    for day in ("2026-08-24", "2026-08-31", "2026-09-07", "2026-09-14"):
        for i in range(40, 160):                       # a usual week grows to 160: 30 is under a quarter
            add_session(conn, "claude", f"{day}-{i}", day)
    tile = stats.overview(conn, TODAY, tmp_path / "none.jsonl")["tiles"]["friction"]
    assert (tile["thin"], tile["prev4_sessions"]) == (True, 160)


def test_backlog_replays_history_by_week(conn):
    add_pattern(conn, 1, "A", first_seen="2026-09-01")
    add_pattern(conn, 2, "B", first_seen="2026-09-15", status="open", streak=2)
    conn.execute("DELETE FROM pattern_history")      # replace the insert triggers' rows
    decide(conn, 1, "2026-09-10")
    for pid, at, before, after in ((1, "2026-09-01", None, "open"), (1, "2026-09-10", "open", "accepted"),
                                   (2, "2026-09-15T12:00:00Z", None, "open")):
        conn.execute("INSERT INTO pattern_history (pattern_id, at, from_status, to_status) "
                     "VALUES (?,?,?,?)", (pid, at, before, after))
    got = stats.backlog(conn, TODAY)
    assert got["weeks"][:3] == [{"week": "2026-08-31", "open": 1, "quiet": 0, "decided": 0},
                                {"week": "2026-09-07", "open": 0, "quiet": 0, "decided": 1},
                                {"week": "2026-09-14", "open": 1, "quiet": 0, "decided": 1}]
    assert got["days_to_decision"] == {"median": 9, "max": 9, "n": 1}


def test_waiting_counts_the_newest_briefings_undecided_patterns(conn):
    for pid, status in ((1, "open"), (2, "open"), (3, "accepted"), (4, "accepted"), (5, "open")):
        add_pattern(conn, pid, f"P{pid}", status=status)
    add_brief(conn, "2026-09-29", 1)                 # an older briefing: never counted
    for pos, pid in enumerate((2, 3, 4, 5), 1):
        add_brief(conn, TODAY, pid, pos)
    decide(conn, 2, "2026-09-29")                    # decided on another briefing: still waiting
    decide(conn, 3, TODAY)                           # decided here
    #  4: accepted here with no proposal ticked, so no verdict row, but not waiting
    decide(conn, 5, TODAY, "comment")                # a comment is an answer too
    add_pattern(conn, 6, "P6", status="open", streak=3)   # carrying but not in the briefing
    assert stats.backlog(conn, TODAY)["waiting"] == 1
    conn.execute("DELETE FROM briefs")
    assert stats.backlog(conn, TODAY)["waiting"] == 0


def test_run_health_dedupes_and_names_timeouts(tmp_path):
    hb = tmp_path / "history.jsonl"
    lines = [{"ts_utc": "2026-09-28T03:32:00Z", "exit": 0, "duration_s": 160, "items": 5},
             {"ts_utc": "2026-09-28T03:32:00Z", "exit": 0, "duration_s": 160, "items": 5},
             {"ts_utc": "2026-09-29T03:32:00Z", "exit": 124, "duration_s": 2400},
             {"ts_utc": "2026-09-30T03:32:00Z", "exit": 1, "duration_s": 3}]
    hb.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    assert [r["status"] for r in stats.run_health(hb)] == ["ok", "timed out", "failed"]


def test_overview_bundles_the_tiles(fixes, tmp_path):
    hb = tmp_path / "history.jsonl"
    hb.write_text(json.dumps({"ts_utc": "2026-09-30T03:32:00Z", "exit": 0, "duration_s": 160}) + "\n")
    got = stats.overview(fixes, TODAY, hb)
    assert got["headline"].startswith("2 of 4 accepted patterns have linked evidence")
    assert got["tiles"]["fixes"] == {"accepted": 4, "with_evidence": 2, "coverage_unavailable": 0}
    assert got["tiles"]["runs"]["ok"] == 1 and got["tiles"]["runs"]["total"] == 1
    assert "2026-09-01" in got["fix_dates"]


def test_snag_counts_reach_weeks_whose_transcripts_are_gone(conn):
    add_runs(conn, days("2026-09-07", 21))                           # 09-28 week uncovered
    add_session(conn, "claude", "c2", "2026-09-22")                  # oldest transcript left
    add_session(conn, "claude", "h1", "2026-09-22", headless=1)
    add_event(conn, "e1", "c1", "claude", "drag", "2026-09-08")      # c1's transcript is gone
    add_event(conn, "e2", "c1", "claude", "drag", "2026-09-16")      # counted once, first week
    add_event(conn, "e3", "c2", "claude", "drag", "2026-09-22")
    add_event(conn, "e4", "h1", "claude", "drag", "2026-09-23")      # headless excluded
    add_event(conn, "e5", "x1", "codex", "drag", "2026-09-23")
    add_event(conn, "e6", "c3", "claude", "drag", "2026-09-29")
    rows = {(r["week"], r["agent"]): r for r in stats.friction_weekly(conn, TODAY)}
    pruned = rows[("2026-09-07", "claude")]
    assert (pruned["gap"], pruned["rate"]) == ("transcripts pruned", None)
    assert (pruned["snag_sessions"], pruned["count_gap"]) == (1, None)
    assert rows[("2026-09-14", "claude")]["snag_sessions"] == 0
    assert rows[("2026-09-21", "claude")]["snag_sessions"] == 1
    assert rows[("2026-09-21", "codex")]["snag_sessions"] == 1
    assert rows[("2026-09-14", "codex")]["count_gap"] == "not tracked yet"
    assert rows[("2026-09-28", "claude")]["count_gap"] == "REM not running"


def test_backlog_counts_open_things_with_no_repeat_in_30_days_as_quiet(conn):
    add_pattern(conn, 1, "A", first_seen="2026-08-01", status="open")
    conn.execute("DELETE FROM pattern_history")
    conn.execute("INSERT INTO pattern_history (pattern_id, at, from_status, to_status) "
                 "VALUES (1, '2026-08-01', NULL, 'open')")
    add_events(conn, 1, ["2026-08-01", "2026-09-10"])
    weeks = {w["week"]: w for w in stats.backlog(conn, TODAY)["weeks"]}
    assert weeks["2026-08-24"]["quiet"] == 0          # week ends 08-30: 29 days since
    assert weeks["2026-08-31"]["quiet"] == 1          # week ends 09-06: 36 days since
    assert weeks["2026-09-07"]["quiet"] == 0          # came back 09-10
    assert weeks["2026-09-07"]["open"] == 1


def test_backfill_runs_cover_days_but_do_not_start_routing(conn):
    conn.execute("INSERT INTO runs (run_date, started_at, window_days, status, finished_at) "
                 "VALUES ('2026-09-02','x',1,'backfill','x')")
    assert stats.routing_start(conn) is None
    assert date(2026, 9, 1) in stats.covered_days(conn)
    add_runs(conn, ["2026-09-20"], window=5)
    assert stats.routing_start(conn) == date(2026, 9, 20)


def test_weeks_with_unjudged_events_wait_for_the_next_shift(conn):
    add_runs(conn, days("2026-09-14", 17))
    add_session(conn, "claude", "c1", "2026-09-15")
    add_session(conn, "claude", "c2", "2026-09-22")
    add_event(conn, "e1", "c1", "claude", "drag", "2026-09-15")
    add_event(conn, "e2", "c2", "claude", "drag", "2026-09-22")
    add_event(conn, "e3", "c2", "claude", None, "2026-09-23")       # extracted, not judged
    rows = {(r["week"], r["agent"]): r for r in stats.friction_weekly(conn, TODAY)}
    assert rows[("2026-09-14", "claude")]["gap"] is None
    late = rows[("2026-09-21", "claude")]
    assert (late["gap"], late["count_gap"], late["rate"]) == ("not judged yet", "not judged yet", None)


def test_partly_covered_weeks_show_their_count_as_partial(conn):
    add_runs(conn, ["2026-09-15", "2026-09-16"])         # 09-14..09-16, plus 09-20 from the next run
    add_runs(conn, days("2026-09-21", 10))
    add_event(conn, "e1", "c1", "claude", "drag", "2026-09-15")
    add_event(conn, "e2", "c2", "claude", "drag", "2026-09-22")
    rows = {(r["week"], r["agent"]): r for r in stats.friction_weekly(conn, TODAY)}
    part = rows[("2026-09-14", "claude")]
    assert (part["gap"], part["count_gap"], part["partial"]) == ("REM not running", None, True)
    assert (part["snag_sessions"], part["covered"], part["days"]) == (1, 4, 7)
    assert rows[("2026-09-21", "claude")]["partial"] is False
    assert rows[("2026-09-14", "codex")]["count_gap"] == "not tracked yet"


def test_a_partial_week_with_no_snags_stays_a_gap(conn):
    add_runs(conn, ["2026-09-15"])                                    # 2 of 7 nights
    add_runs(conn, days("2026-09-22", 9))
    add_event(conn, "e0", "c0", "claude", "lift", "2026-09-15")     # watched, no snag
    add_event(conn, "e1", "c1", "claude", "drag", "2026-09-23")
    rows = {(r["week"], r["agent"]): r for r in stats.friction_weekly(conn, TODAY)}
    assert rows[("2026-09-14", "claude")]["count_gap"] == "REM not running"   # a floor of 0 says nothing


# ---- the honest night-shift state ------------------------------------------------------------

def heartbeat(tmp_path, rows):
    hb = tmp_path / "history.jsonl"
    hb.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return hb


def add_run_row(c, started, finished=None, status=None, sessions=0):
    c.execute("INSERT INTO runs (run_date, started_at, finished_at, sessions_scanned, status) "
              "VALUES (?,?,?,?,?)", (started[:10], started, finished, sessions,
                                     status or ("extracted" if finished else "running")))


def four_nights_ending_stuck(conn, tmp_path):
    """Four nights: a good night, a timeout, a crash, then a clean exit whose
    run row never finished."""
    add_run_row(conn, "2026-10-24T03:30:00+00:00", "2026-10-24T03:33:00+00:00", sessions=1000)
    add_run_row(conn, "2026-10-27T04:45:00+00:00")                         # left 'running', 0 read
    return heartbeat(tmp_path, [
        {"ts_utc": "2026-10-24T03:35:00Z", "exit": 0, "duration_s": 300, "items": 5},
        {"ts_utc": "2026-10-25T05:10:00Z", "exit": 124, "duration_s": 2400},
        {"ts_utc": "2026-10-26T04:55:00Z", "exit": 1, "duration_s": 1000},
        {"ts_utc": "2026-10-27T05:00:00Z", "exit": 0, "duration_s": 1100}])


def test_a_clean_exit_whose_run_never_finished_got_stuck(conn, tmp_path):
    hb = four_nights_ending_stuck(conn, tmp_path)
    add_run_row(conn, "2026-10-27T12:00:00+00:00", "2026-10-27T12:03:00+00:00", sessions=900)  # run now
    now = datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc)
    runs = stats.run_health(hb, conn, now=now)
    assert [r["status"] for r in runs] == ["ok", "timed out", "failed", "stuck"]
    assert runs[-1]["sessions"] == 0 and not runs[-1].get("open")
    assert [r["status"] for r in stats.run_health(hb)] == ["ok", "timed out", "failed", "ok"]


def test_shift_health_counts_every_miss_since_the_last_good_night(conn, tmp_path):
    hb = four_nights_ending_stuck(conn, tmp_path)
    (tmp_path / "last-run.txt").write_text("Waiting on extract.\n")
    got = stats.overview(conn, "2026-10-27", hb, now=datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc))
    shift = got["shift"]
    assert shift["state"] == "failing"
    assert [r["status"] for r in shift["missed"]] == ["timed out", "failed", "stuck"]
    assert shift["last_ok"] == "2026-10-24T03:35:00Z" and shift["current"] is None
    assert shift["log"]["text"] == "Waiting on extract." and shift["log"]["at"].endswith("Z")
    assert got["tiles"]["runs"] == {"ok": 1, "total": 4, "missed": 3, "last": got["runs"][-1]}


def test_an_open_run_is_on_shift_only_until_the_stuck_threshold(conn, tmp_path):
    hb = heartbeat(tmp_path, [{"ts_utc": "2026-10-26T03:33:00Z", "exit": 0, "duration_s": 150}])
    add_run_row(conn, "2026-10-26T03:30:40+00:00", "2026-10-26T03:32:50+00:00")
    add_run_row(conn, "2026-10-27T03:30:00+00:00")                         # no heartbeat after it
    start = datetime(2026, 10, 27, 3, 30, tzinfo=timezone.utc)

    fresh = stats.run_health(hb, conn, now=start + timedelta(minutes=12))
    assert (fresh[-1]["status"], fresh[-1]["open"], fresh[-1]["duration_s"]) == ("running", True, 720)
    shift = stats.shift_health(fresh)
    assert (shift["state"], shift["missed"], shift["current"]["status"]) == ("running", [], "running")

    late = stats.run_health(hb, conn, now=start + timedelta(minutes=stats.STUCK_MINUTES + 1))
    assert late[-1]["status"] == "stuck"
    shift = stats.shift_health(late)
    assert shift["state"] == "failing" and [r["status"] for r in shift["missed"]] == ["stuck"]
    assert shift["current"]["status"] == "stuck"


def test_a_good_last_night_is_ok_and_an_open_backfill_is_not_a_shift(conn, tmp_path):
    hb = heartbeat(tmp_path, [{"ts_utc": "2026-10-25T05:10:00Z", "exit": 124, "duration_s": 2400},
                              {"ts_utc": "2026-10-26T03:33:00Z", "exit": 0, "duration_s": 150}])
    add_run_row(conn, "2026-10-26T03:30:40+00:00", "2026-10-26T03:32:50+00:00")
    conn.execute("INSERT INTO runs (run_date, started_at, status) VALUES "
                 "('2026-09-02', '2026-10-27T09:00:00+00:00', 'backfill')")    # never finished
    runs = stats.run_health(hb, conn, now=datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc))
    assert [r["status"] for r in runs] == ["timed out", "ok"]
    shift = stats.shift_health(runs)
    assert (shift["state"], shift["missed"], shift["log"]) == ("ok", [], None)
    assert stats.shift_health([])["state"] == "none"


# ---- fixes you said yes to -------------------------------------------------------------------

def add_proposal(c, prop_id, pid, tier, text):
    c.execute("INSERT INTO proposals (id, pattern_id, tier, text) VALUES (?,?,?,?)",
              (prop_id, pid, tier, text))


def say_yes(c, pid, prop_id, day, comment=""):
    decide(c, pid, day, proposal_id=prop_id, comment=comment)


def test_accepted_fixes_lists_unmade_yeses_first_with_their_fix_idea(conn):
    add_pattern(conn, 1, "Not made yet")
    add_proposal(conn, 11, 1, 1, "Add a rule")
    say_yes(conn, 1, 11, "2026-10-27", comment="do it tonight")
    add_pattern(conn, 2, "Made")
    add_proposal(conn, 21, 2, 1, "Fix the extractor")
    say_yes(conn, 2, 21, "2026-10-27")
    conn.execute("UPDATE patterns SET fix_pr='https://example.com/pull/15' WHERE id=2")
    add_pattern(conn, 3, "Two ideas, back on my list", status="open")
    add_proposal(conn, 32, 3, 3, "Add a selfcheck")
    add_proposal(conn, 31, 3, 1, "Skip those turns")
    say_yes(conn, 3, 32, "2026-10-20")
    say_yes(conn, 3, 31, "2026-10-20")
    add_pattern(conn, 4, "Idea rewritten since")
    say_yes(conn, 4, 999, "2026-10-11")                                   # proposal row is gone

    got = stats.accepted_fixes(conn, "2026-10-27")
    assert [r["pattern_id"] for r in got] == [1, 3, 4, 2]                 # unmade, newest first
    first = got[0]
    assert (first["title"], first["decided"], first["age_days"], first["fix_pr"], first["back"]) == \
        ("Not made yet", "2026-10-27", 0, None, False)
    assert first["ideas"] == [{"tier": 1, "text": "Add a rule"}] and first["note"] == "do it tonight"
    assert [i["text"] for i in got[1]["ideas"]] == ["Skip those turns", "Add a selfcheck"]
    assert got[1]["back"] is True and got[1]["age_days"] == 7
    assert got[2]["ideas"] == []
    assert got[3]["fix_pr"] == "https://example.com/pull/15"


def test_accepted_fixes_keep_the_newest_yes_and_skip_old_or_withdrawn_ones(conn):
    add_pattern(conn, 1, "Said yes twice")
    add_proposal(conn, 11, 1, 1, "First try")
    add_proposal(conn, 12, 1, 2, "Second try")
    say_yes(conn, 1, 11, "2026-10-11")
    say_yes(conn, 1, 12, "2026-10-27")
    add_pattern(conn, 2, "Too old")
    add_proposal(conn, 21, 2, 1, "Old idea")
    say_yes(conn, 2, 21, "2026-09-26")                                    # 31 days back
    add_pattern(conn, 3, "Later dropped", status="dropped")
    add_proposal(conn, 31, 3, 1, "Never mind")
    say_yes(conn, 3, 31, "2026-10-25")
    add_pattern(conn, 4, "Only a nah")
    decide(conn, 4, "2026-10-26", "drop")

    got = stats.accepted_fixes(conn, "2026-10-27")
    assert [(r["pattern_id"], r["decided"]) for r in got] == [(1, "2026-10-27")]
    assert got[0]["ideas"] == [{"tier": 2, "text": "Second try"}]
    assert [r["pattern_id"] for r in stats.accepted_fixes(conn, "2026-10-27", days=31)] == [1, 2]


def test_overview_carries_the_shift_and_the_yeses(fixes, tmp_path):
    got = stats.overview(fixes, TODAY, tmp_path / "none.jsonl",
                         now=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))
    assert got["shift"]["state"] == "none" and got["shift"]["missed"] == []
    assert [r["title"] for r in got["accepted"]] == ["Early", "Quiet", "Worse", "Thin"]


def test_coordinator_runs_work_without_legacy_heartbeat(conn, tmp_path):
    conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status, sessions_scanned, "
                 "patterns_surfaced, provenance_json) VALUES "
                 "('2026-09-30','2026-09-30T06:30:00Z','2026-09-30T06:32:00Z','ok',4,2,?)",
                 (json.dumps({'configured_model': 'test'}),))
    runs = stats.run_health(tmp_path / 'absent.jsonl', conn)
    assert len(runs) == 1
    assert (runs[0]['status'], runs[0]['sessions'], runs[0]['items']) == ('ok', 4, 2)
    assert stats.shift_health(runs)['state'] == 'ok'


def test_coordinator_failure_overrides_stale_success_heartbeat(conn, tmp_path):
    conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status, provenance_json) "
                 "VALUES ('2026-09-30','2026-09-30T06:30:00Z','2026-09-30T06:32:00Z','failed',?)",
                 (json.dumps({'configured_model': 'test'}),))
    hb = tmp_path / 'history.jsonl'
    hb.write_text(json.dumps({'ts_utc': '2026-09-30T06:33:00Z', 'exit': 0}))
    (tmp_path / 'last-run.txt').write_text('unrelated legacy note')
    runs = stats.run_health(hb, conn)
    assert len(runs) == 1 and runs[0]['status'] == 'failed'
    assert stats.shift_health(runs, hb)['log'] is None


def test_open_coordinator_run_becomes_stuck(conn, tmp_path):
    conn.execute("INSERT INTO runs (run_date, started_at, status, provenance_json) "
                 "VALUES ('2026-09-30','2026-09-30T06:30:00Z','running',?)",
                 (json.dumps({'configured_model': 'test'}),))
    runs = stats.run_health(tmp_path / 'absent', conn,
                            now=datetime(2026, 9, 30, 8, tzinfo=timezone.utc))
    assert len(runs) == 1 and runs[0]['status'] == 'stuck'


def test_failed_or_running_night_does_not_create_coverage(conn):
    add_pattern(conn, 1, 'Awaiting observations')
    decide(conn, 1, '2026-09-01')
    for status, finish in [('running', None), ('failed', '2026-09-30T06:31:00Z')]:
        conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status, window_days) "
                     "VALUES ('2026-09-30','2026-09-30T06:30:00Z',?,?,5)", (finish, status))
    assert stats.covered_days(conn) == set()
    assert stats.routing_start(conn) is None
    row, = stats.fix_stuck(conn, TODAY)
    assert row['label'] == 'coverage unavailable' and row['observed_after'] == 0


def test_acceptance_followup_includes_pattern_previously_dropped(conn):
    add_pattern(conn, 1, 'Accepted after dismissal')
    decide(conn, 1, '2026-09-01', 'drop')
    decide(conn, 1, '2026-09-10', 'accept')
    add_runs(conn, ['2026-09-10', '2026-09-11'])
    add_events(conn, 1, ['2026-09-11'])
    assert stats.first_decisions(conn)[1]['decision'] == 'drop'
    row, = stats.fix_stuck(conn, TODAY)
    assert row['pattern_id'] == 1 and row['decided'] == '2026-09-10'
    assert row['after'] == 1


def test_a_failed_run_covers_no_days(conn):
    conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status, window_days) "
                 "VALUES ('2026-09-20', 'x', 'y', 'failed', 14)")
    conn.execute("INSERT INTO runs (run_date, started_at, finished_at, status, window_days) "
                 "VALUES ('2026-09-21', 'x', 'y', 'extracted', 14)")
    assert stats.run_windows(conn) == [(date(2026, 9, 21), 14)]
