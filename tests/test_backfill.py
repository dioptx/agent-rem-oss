"""Backfills: status history from verdicts and surviving sessions."""

from __future__ import annotations

import pytest

from rem import backfill, config
from rem.db import ConsolidationDB
from tests.test_extract import user, write_session


@pytest.fixture
def db(tmp_path):
    database = ConsolidationDB(tmp_path / "events.db")
    yield database
    database.close()


def legacy_pattern(db, pid, first_seen, status="open"):
    """A pattern created before pattern_history existed: no history rows (the insert trigger's
    row is removed to simulate that)."""
    db.conn.execute(
        "INSERT INTO patterns (id, signature, axis, valence, title, first_seen, last_seen, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (pid, f"s{pid}", "friction", "drag", f"P{pid}", first_seen, first_seen, status))
    db.conn.execute("DELETE FROM pattern_history WHERE pattern_id=?", (pid,))


def test_history_backfill_seeds_a_legacy_pattern_that_already_has_a_trigger_row(db):
    legacy_pattern(db, 4, "2026-06-01")
    db.conn.execute("UPDATE patterns SET status='muted' WHERE id=4")    # old code, after triggers
    backfill.history(db)
    got = [tuple(r) for r in db.conn.execute(
        "SELECT from_status, to_status FROM pattern_history WHERE pattern_id=4 ORDER BY at")]
    assert got[0] == (None, "open") and got[-1] == ("open", "muted")
    assert len(got) == 2                                                 # no duplicate closing row


def verdict(db, pid, run_date, decision, created_at):
    db.conn.execute(
        "INSERT INTO verdicts (pattern_id, run_date, decision, created_at) VALUES (?,?,?,?)",
        (pid, run_date, decision, created_at))


def rows(db):
    return [(r["pattern_id"], r["at"], r["from_status"], r["to_status"]) for r in db.conn.execute(
        "SELECT * FROM pattern_history ORDER BY pattern_id, id")]


def test_history_backfill_replays_verdicts_once(db):
    legacy_pattern(db, 1, "2026-06-10", "accepted")
    legacy_pattern(db, 2, "2026-06-11")
    verdict(db, 1, "2026-06-13", "accept", "2026-06-14T03:00:00+00:00")
    verdict(db, 2, "2026-06-13", "comment", "2026-06-14T03:00:00+00:00")   # not a status change
    assert backfill.history(db) == 3
    assert rows(db) == [(1, "2026-06-10", None, "open"), (1, "2026-06-14", "open", "accepted"),
                        (2, "2026-06-11", None, "open")]
    assert backfill.history(db) == 0


def test_history_backfill_ends_on_the_current_status(db):
    legacy_pattern(db, 3, "2026-06-01")                  # accepted, then recurred and reopened
    db.conn.execute("UPDATE patterns SET last_seen='2026-06-20' WHERE id=3")
    verdict(db, 3, "2026-06-05", "accept", "2026-06-06T03:00:00+00:00")
    backfill.history(db)
    assert rows(db)[-1] == (3, "2026-06-20", "accepted", "open")


def test_sessions_backfill_records_each_surviving_transcript_once(tmp_path, monkeypatch, db):
    write_session(tmp_path / "t", "s1", [user("hi", ts="2026-06-01T10:00:00Z")])
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", tmp_path / "t")
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(config, "CODEX_ARCHIVE_DIR", tmp_path / "none")
    assert backfill.sessions(db, "2026-06-13") == 1
    assert backfill.sessions(db, "2026-06-13") == 0
    assert db.conn.execute("SELECT started_date FROM sessions").fetchone()[0] == "2026-06-01"


def test_history_closing_row_never_precedes_creation(db):
    legacy_pattern(db, 86, "2026-09-18", "accepted")
    db.conn.execute("UPDATE patterns SET last_seen='2026-09-14', last_verdict_at='2026-09-18' WHERE id=86")
    db.conn.execute("DELETE FROM pattern_history WHERE pattern_id=86")   # trigger logged the status set
    backfill.history(db)
    got = [(r["at"], r["from_status"], r["to_status"]) for r in db.conn.execute(
        "SELECT * FROM pattern_history WHERE pattern_id=86 ORDER BY at, id")]
    assert got[-1][1:] == ("open", "accepted")
    assert all(at >= "2026-09-18" for at, _, _ in got)


def test_history_backfill_does_not_repeat_changes_a_trigger_already_logged(db):
    legacy_pattern(db, 7, "2026-06-01")
    verdict(db, 7, "2026-09-19", "drop", "2026-09-20T03:00:00+00:00")
    db.conn.execute("UPDATE patterns SET status='dropped' WHERE id=7")
    backfill.history(db)
    got = [(r["from_status"], r["to_status"]) for r in db.conn.execute(
        "SELECT * FROM pattern_history WHERE pattern_id=7 ORDER BY at, id")]
    assert got.count(("open", "dropped")) == 1
    assert got[0] == (None, "open")
    assert all(a[1] == b[0] for a, b in zip(got, got[1:]))
