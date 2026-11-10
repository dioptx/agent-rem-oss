"""Session denominator: every scanned session is recorded once with its first day."""

from __future__ import annotations

from rem import config, extract
from rem.db import ConsolidationDB
from tests.test_extract import TODAY, user, write_session


def test_scan_reports_the_first_day(tmp_path):
    f = write_session(tmp_path, "s1", [user("hi", ts="2026-06-11T10:00:00Z"),
                                       user("more", ts="2026-06-12T10:00:00Z")])
    assert extract.scan_file(f, "2026-06-08", TODAY)["started_date"] == "2026-06-11"


def test_run_records_sessions_and_its_window(tmp_path, monkeypatch):
    write_session(tmp_path, "s1", [user("No, that's wrong")])
    write_session(tmp_path, "s2", [user("[AUTOMATED HEADLESS RUN] do the job")])
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(config, "CODEX_ARCHIVE_DIR", tmp_path / "none")
    db = ConsolidationDB(tmp_path / "db" / "events.db")
    extract.run(TODAY, 5, db)
    rows = {r["session_id"]: r["headless"] for r in db.conn.execute("SELECT * FROM sessions")}
    assert rows == {"s1": 0, "s2": 1}
    assert db.conn.execute("SELECT window_days FROM runs").fetchone()[0] == 5
    db.close()


def test_upsert_keeps_the_earliest_start(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    db.upsert_session("claude", "s1", "2026-06-12", False)
    db.upsert_session("claude", "s1", "2026-06-10", False)
    db.upsert_session("claude", "s1", None, False)
    assert db.conn.execute("SELECT started_date FROM sessions").fetchone()[0] == "2026-06-10"
    db.close()


def test_upsert_fills_a_missing_start(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    db.upsert_session("claude", "s1", None, False)
    db.upsert_session("claude", "s1", "2026-06-10", False)
    assert db.conn.execute("SELECT started_date FROM sessions").fetchone()[0] == "2026-06-10"
    db.close()


def test_upsert_headless_ratchets_up(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    db.upsert_session("claude", "s1", "2026-06-10", False)
    db.upsert_session("claude", "s1", "2026-06-10", True)
    assert db.conn.execute("SELECT headless FROM sessions").fetchone()[0] == 1
    db.close()


def test_backfill_run_is_marked(tmp_path, monkeypatch):
    write_session(tmp_path, "s1", [user("No, that's wrong")])
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(config, "CODEX_ARCHIVE_DIR", tmp_path / "none")
    db = ConsolidationDB(tmp_path / "db" / "events.db")
    extract.run(TODAY, 1, db, backfill=True)
    assert db.conn.execute("SELECT status FROM runs").fetchone()[0] == "backfill"
    db.close()


def _setup(tmp_path, monkeypatch):
    write_session(tmp_path, "s1", [user("No, that's wrong")])
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(config, "CODEX_ARCHIVE_DIR", tmp_path / "none")
    return ConsolidationDB(tmp_path / "db" / "events.db")


def test_a_run_that_dies_closes_its_row_as_failed(tmp_path, monkeypatch):
    import pytest
    db = _setup(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise SystemExit(143)          # what the SIGTERM handler raises at teardown
    monkeypatch.setattr(extract, "scan_file", boom)
    with pytest.raises(SystemExit):
        extract.run(TODAY, 5, db)
    row = db.conn.execute("SELECT status, finished_at, error FROM runs").fetchone()
    assert row["status"] == "failed" and row["finished_at"] and "SystemExit" in row["error"]
    db.close()


def test_the_next_start_closes_a_row_left_open_by_a_kill(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    db.conn.execute("INSERT INTO runs (run_date, started_at) VALUES ('2026-06-10', '2026-06-10T05:00:00+00:00')")
    fresh = db.start_run("2026-06-12")   # still within the hour: left alone
    extract.run(TODAY, 5, db)
    rows = {r["run_date"]: (r["status"], r["error"]) for r in db.conn.execute("SELECT * FROM runs")}
    assert rows["2026-06-10"][0] == "failed" and "abandoned" in rows["2026-06-10"][1]
    assert db.conn.execute("SELECT status FROM runs WHERE id=?", (fresh,)).fetchone()[0] == "running"
    db.close()


def test_the_signal_handler_turns_teardown_into_a_clean_exit():
    import pytest
    import signal
    with pytest.raises(SystemExit) as e:
        extract._exit_on_signal(signal.SIGTERM, None)
    assert e.value.code == 128 + signal.SIGTERM
