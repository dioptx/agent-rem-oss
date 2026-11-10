"""The judge sees recently decided patterns, and a recurrence reattaches to them."""

from __future__ import annotations

import pytest

from rem import judge
from rem.db import ConsolidationDB
from tests.test_lifecycle import TODAY, add_candidate, cluster_one, d


@pytest.fixture
def db(tmp_path):
    database = ConsolidationDB(tmp_path / "events.db")
    yield database
    database.close()


def decided(db, signature, status, when):
    add_candidate(db, f"{signature}-1", "error", d(20))
    cluster_one(db, f"{signature}-1", signature, signature)
    pid = db.find_pattern_by_signature(signature)["id"]
    db.set_pattern_status(pid, status)
    db.mark_resolved(pid, when)
    return pid


def recur(db, eid, pid):
    add_candidate(db, eid, "error", d(0))
    judge.cmd_cluster(db, TODAY, {"events": [{
        "id": eid, "axis": "friction", "valence": "drag",
        "signature": "reworded by the model", "existing_pattern_id": pid}], "patterns": {}})


def test_dump_offers_recently_decided_patterns(db):
    recent = decided(db, "patch misses", "accepted", d(5))
    decided(db, "old fix", "accepted", "2026-01-02")          # outside 90 days
    dump = judge.cmd_dump(db, TODAY)
    assert [p["id"] for p in dump["decided_patterns"]] == [recent]
    assert dump["decided_patterns"][0]["status"] == "accepted"
    assert dump["decided_patterns"][0]["axis"] == "friction"
    assert dump["decided_patterns"][0]["valence"] == "drag"


def test_recurrence_on_an_accepted_pattern_reopens_it(db):
    pid = decided(db, "patch misses", "accepted", d(5))
    recur(db, "again", pid)
    row = db.get_pattern(pid)
    assert row["status"] == "open" and row["prior_decision"] == "accepted"
    assert db.conn.execute("SELECT pattern_id FROM events WHERE id='again'").fetchone()[0] == pid


def test_recurrence_on_a_muted_pattern_stays_muted(db):
    pid = decided(db, "noise", "muted", d(5))
    recur(db, "again", pid)
    assert db.get_pattern(pid)["status"] == "muted"
    assert db.conn.execute("SELECT pattern_id FROM events WHERE id='again'").fetchone()[0] == pid


def test_unknown_existing_id_falls_back_to_the_signature(db):
    add_candidate(db, "lost", "error", d(0))
    out = judge.cmd_cluster(db, TODAY, {"events": [{
        "id": "lost", "axis": "friction", "valence": "drag",
        "signature": "brand new thing", "existing_pattern_id": 999}], "patterns": {}})
    pid = db.conn.execute("SELECT pattern_id FROM events WHERE id='lost'").fetchone()[0]
    assert out["patterns_created"] == 1 and pid != 999
    assert db.get_pattern(999) is None
    assert db.get_pattern(pid)["signature"] == "brand new thing"


def test_event_dated_on_the_decision_day_does_not_reopen(db):
    pid = decided(db, "patch misses", "accepted", d(5))
    add_candidate(db, "same-day", "error", d(5))
    judge.cmd_cluster(db, TODAY, {"events": [{
        "id": "same-day", "axis": "friction", "valence": "drag",
        "signature": "patch misses", "existing_pattern_id": pid}], "patterns": {}})
    assert db.get_pattern(pid)["status"] == "accepted"
    assert db.conn.execute("SELECT pattern_id FROM events WHERE id='same-day'").fetchone()[0] == pid


def test_recurrence_on_a_dropped_pattern_reopens_it(db):
    pid = decided(db, "stale idea", "dropped", d(5))
    recur(db, "again", pid)
    row = db.get_pattern(pid)
    assert row["status"] == "open" and row["prior_decision"] == "dropped"


def test_dump_excludes_a_decided_pattern_that_reopened(db):
    pid = decided(db, "patch misses", "accepted", d(5))
    recur(db, "again", pid)
    assert judge.cmd_dump(db, TODAY)["decided_patterns"] == []
