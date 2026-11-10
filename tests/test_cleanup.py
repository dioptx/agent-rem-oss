import json
import sqlite3

import pytest

from rem.cleanup import apply, plan
from rem.db import ConsolidationDB
from rem.extract import _event_id


@pytest.fixture
def setup(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    # Frozen fixture date: the manifest must select only these historical rows.
    day = "2026-10-01"
    event = dict(id=_event_id("claude", "session", day, "error"), agent="claude",
                 session_id="session", occurred_date=day, run_date=day, kind="error",
                 evidence=["bad syntax"], signature="syntax", pattern_id=1, signal_count=1)
    db.create_pattern("syntax", "friction", "drag", "Syntax", day)
    db.insert_event(event)
    db.insert_event({**event, "id": "old"})
    evidence = {"window": {"start": day, "end": day},
                "exact_duplicate_clusters": [[event["id"], "old"]]}
    yield db, event, evidence, tmp_path
    db.close()


def test_duplicate_preserves_history_and_repeat_import(setup):
    db, event, evidence, projects = setup
    db.record_verdict(1, event["run_date"], "accept", comment="keep history")
    before = [tuple(r) for r in db.conn.execute("SELECT * FROM verdicts")]
    assert len(plan(db.conn, evidence, projects)["archive"]) == 1
    assert db.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    apply(db, evidence, projects)
    archived = db.conn.execute("SELECT row_json FROM event_archive").fetchone()[0]
    assert json.loads(archived)["id"] == "old"
    assert before == [tuple(r) for r in db.conn.execute("SELECT * FROM verdicts")]
    db.insert_event({**event, "id": "old", "evidence": ["new evidence"], "signal_count": 2})
    assert db.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    row = db.conn.execute("SELECT * FROM events").fetchone()
    assert row["id"] == event["id"]
    assert json.loads(row["evidence_json"]) == ["new evidence"]
    assert apply(db, evidence, projects)["archive"] == []
    with pytest.raises(sqlite3.IntegrityError, match="Archived event"):
        db.conn.execute("INSERT INTO events (id, run_date, occurred_date) VALUES ('old', 'x', 'x')")
    db.conn.rollback()


def test_conflicting_classification_is_preserved(setup):
    db, event, evidence, projects = setup
    db.conn.execute("UPDATE events SET signature='different' WHERE id='old'")
    db.conn.commit()
    result = apply(db, evidence, projects)
    assert result["archive"] == []
    assert result["skipped"][0]["reason"] == "identity or classification differs"


def test_only_verified_test_directory_is_archived(setup):
    db, event, evidence, projects = setup
    evidence["exact_duplicate_clusters"] = []
    evidence["test_candidates"] = [event]
    real = projects / "home"
    real.mkdir()
    source = real / "session.jsonl"
    source.write_text("{}\n")
    assert plan(db.conn, evidence, projects)["archive"] == []
    test = projects / "project-scratchpad-bench-ws-fixture"
    test.mkdir()
    source.rename(test / source.name)
    result = apply(db, evidence, projects)
    assert len(result["archive"]) == 1
    db.insert_event(event)
    assert db.conn.execute("SELECT count(*) FROM events WHERE id=?", (event["id"],)).fetchone()[0] == 0
    assert json.loads(db.conn.execute("SELECT row_json FROM event_archive").fetchone()[0])["evidence_json"]


def test_stale_review_rolls_back(setup):
    db, event, evidence, projects = setup
    evidence["test_candidates"] = [{**event, "evidence": ["not reviewed"]}]
    with pytest.raises(ValueError, match="Reviewed evidence changed"):
        apply(db, evidence, projects)
    assert db.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    assert db.conn.execute("SELECT count(*) FROM event_archive").fetchone()[0] == 0
