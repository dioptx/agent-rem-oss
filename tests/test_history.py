"""Append-only status history, settings log, and decision times that survive re-review."""

from __future__ import annotations

import pytest

from rem import feedback
from rem.db import ConsolidationDB


@pytest.fixture
def db(tmp_path):
    database = ConsolidationDB(tmp_path / "events.db")
    yield database
    database.close()


def history(db, pid):
    return [(r["from_status"], r["to_status"], r["source"]) for r in db.conn.execute(
        "SELECT * FROM pattern_history WHERE pattern_id=? ORDER BY id", (pid,))]


def test_status_changes_are_logged_in_order(db):
    pid = db.create_pattern("sig", "friction", "drag", "Sig", "2026-06-10")
    db.set_pattern_status(pid, "accepted")
    db.reopen_with_context(pid, "2026-06-12", "accepted")
    db.set_pattern_status(pid, "snoozed", snoozed_until="2026-06-20")
    db.reactivate_pattern(pid, "2026-06-21")
    assert history(db, pid) == [
        (None, "open", "created"),
        ("open", "accepted", "verdict"),
        ("accepted", "open", "recurred"),
        ("open", "snoozed", "verdict"),
        ("snoozed", "open", "recurred"),
    ]


def test_unchanged_status_logs_nothing(db):
    pid = db.create_pattern("sig", "friction", "drag", "Sig", "2026-06-10")
    db.set_pattern_status(pid, "open")
    db.reactivate_pattern(pid, "2026-06-11")          # not snoozed: a no-op
    assert history(db, pid) == [(None, "open", "created")]


JOURNAL = """## 1 ▼ DRAG · friction · Sig · ×3 (score 3) `[Day 1 · unresolved]` <!--P:{pid}-->
**Proposals**
- [x] T1 — do the thing <!--p:{prop}-->
  note:

---
"""


def test_rereview_keeps_the_first_decision_time(db):
    pid = db.create_pattern("sig", "friction", "drag", "Sig", "2026-06-10")
    prop = db.add_proposal(pid, 1, "do the thing")
    verdicts = feedback.parse_journal(JOURNAL.format(pid=pid, prop=prop))
    feedback.apply(db, verdicts, "2026-06-13", "2026-06-13", bump_carries=False)
    first = db.conn.execute("SELECT created_at FROM verdicts").fetchone()["created_at"]
    feedback.apply(db, verdicts, "2026-06-13", "2026-06-14", bump_carries=False)
    assert [r["created_at"] for r in db.conn.execute("SELECT created_at FROM verdicts")] == [first]


def test_settings_log_records_each_change(db):
    db.log_setting("surface_limit", 5, 4, "ui")
    row = db.conn.execute("SELECT * FROM settings_log").fetchone()
    assert (row["key"], row["old"], row["new"], row["source"]) == ("surface_limit", "5", "4", "ui")


def test_reactivating_a_non_snoozed_pattern_logs_nothing(db):
    pid = db.create_pattern("sig", "friction", "drag", "Sig", "2026-06-10")
    db.set_pattern_status(pid, "accepted")
    db.reactivate_pattern(pid, "2026-06-11")
    assert history(db, pid) == [(None, "open", "created"), ("open", "accepted", "verdict")]


DROP_JOURNAL = """## 1 ▼ DRAG · friction · Sig · ×3 (score 3) `[Day 1 · unresolved]` <!--P:{pid}-->
**Proposals**
- [ ] T1 — do the thing <!--p:{prop}-->
  note: drop

---
"""


def test_rereview_keeps_first_time_for_a_drop(db):
    pid = db.create_pattern("sig", "friction", "drag", "Sig", "2026-06-10")
    prop = db.add_proposal(pid, 1, "do the thing")
    verdicts = feedback.parse_journal(DROP_JOURNAL.format(pid=pid, prop=prop))
    feedback.apply(db, verdicts, "2026-06-13", "2026-06-13", bump_carries=False)
    first = db.conn.execute("SELECT created_at FROM verdicts").fetchone()["created_at"]
    feedback.apply(db, verdicts, "2026-06-13", "2026-06-14", bump_carries=False)
    rows = db.conn.execute("SELECT decision, created_at FROM verdicts").fetchall()
    assert [(r["decision"], r["created_at"]) for r in rows] == [("drop", first)]


def test_raw_sql_writes_are_logged_too(db):
    pid = db.create_pattern("sig", "friction", "drag", "Sig", "2026-06-10")
    db.conn.execute("UPDATE patterns SET status='muted' WHERE id=?", (pid,))
    assert ("open", "muted", "verdict") in history(db, pid)
