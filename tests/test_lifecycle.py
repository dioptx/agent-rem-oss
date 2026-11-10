"""Lifecycle behaviors added on top of the base pipeline:

- severity: a concrete tool error outranks noisy keyword corrections;
- mute: silencing a signature survives the nightly LLM re-clustering;
- close-the-loop: an accepted/dropped signature that recurs reopens carrying
  its prior verdict, so the briefing can flag "you already acted on this".
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rem import actions, feedback, judge, report
from rem.db import ConsolidationDB

TODAY = "2026-06-13"


def d(days_ago):
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def db(tmp_path):
    database = ConsolidationDB(tmp_path / "events.db")
    yield database
    database.close()


def add_candidate(db, eid, kind, day):
    db.insert_event({
        "id": eid, "run_date": TODAY, "occurred_date": day, "session_id": eid,
        "project": "example-data", "kind": kind, "title": f"{kind}",
        "detail": f"1x in {eid}", "evidence": [f"ev {eid}"], "raw_weight": 1,
    })


def cluster_one(db, eid, signature, key, valence="drag"):
    return judge.cmd_cluster(db, TODAY, {
        "events": [{"id": eid, "axis": "friction", "valence": valence,
                    "signature": signature, "pattern_key": key}],
        "patterns": {key: {"signature": signature, "axis": "friction",
                           "valence": valence, "title": signature.title()}},
    })


# ---- severity -------------------------------------------------------------

def test_error_outranks_correction_at_equal_recency(db):
    err = db.create_pattern("err sig", "friction", "drag", "Errors", TODAY)
    cor = db.create_pattern("cor sig", "friction", "drag", "Corrections", TODAY)
    for i, da in enumerate([1, 1, 1]):
        db.insert_event({"id": f"err{i}", "run_date": TODAY, "occurred_date": d(da),
                         "kind": "error", "pattern_id": err, "signature": "err sig", "title": "x"})
        db.insert_event({"id": f"cor{i}", "run_date": TODAY, "occurred_date": d(da),
                         "kind": "correction", "pattern_id": cor, "signature": "cor sig", "title": "x"})
    db.recompute_stats(TODAY, 5)
    # identical recency and count; only kind severity separates them
    assert (db.get_pattern(err)["weighted_score"]
            > db.get_pattern(cor)["weighted_score"])
    assert [p["signature"] for p in db.surface()][:2] == ["err sig", "cor sig"]


# ---- mute stickiness ------------------------------------------------------

def test_mute_sticks_across_reclustering(db):
    add_candidate(db, "e1", "correction", d(1))
    cluster_one(db, "e1", "noisy sig", "k")
    pid = db.find_pattern_by_signature("noisy sig")["id"]
    db.set_pattern_status(pid, "muted")

    # a new event with the SAME signature arrives on a later run. The LLM never
    # saw the muted pattern (dump is open-only), so it mints a fresh key.
    add_candidate(db, "e2", "correction", d(0))
    res = cluster_one(db, "e2", "noisy sig", "k2")

    assert res["patterns_created"] == 0                 # reattached, not duped
    assert db.get_pattern(pid)["status"] == "muted"     # stays silenced
    db.recompute_stats(TODAY, 5)
    assert all(p["id"] != pid for p in db.surface())    # never surfaces


# ---- close-the-loop -------------------------------------------------------

def test_accepted_signature_reopens_with_context(db):
    add_candidate(db, "a1", "correction", d(2))
    cluster_one(db, "a1", "recurring fix", "k")
    pid = db.find_pattern_by_signature("recurring fix")["id"]
    db.set_pattern_status(pid, "accepted")

    add_candidate(db, "a2", "correction", d(0))
    res = cluster_one(db, "a2", "recurring fix", "k2")

    assert res["patterns_created"] == 0
    row = db.get_pattern(pid)
    assert row["status"] == "open"
    assert row["prior_decision"] == "accepted"
    assert row["surfaced_streak"] == 0                  # comes back fresh, Day 1

    db.recompute_stats(TODAY, 5)
    assert any(p["id"] == pid for p in db.surface())
    db.add_brief(TODAY, pid, 1, "ev", "pat")
    assert "recurred after you accepted it" in report.render(db, TODAY)


def test_recurrence_names_the_fix_pr_and_its_state(db, monkeypatch):
    monkeypatch.setattr(actions, "fix_state", lambda url: "merged")
    add_candidate(db, "f1", "correction", d(2))
    cluster_one(db, "f1", "env assumed", "k")
    pid = db.find_pattern_by_signature("env assumed")["id"]
    db.set_pattern_status(pid, "accepted")
    db.set_fix_pr(pid, "https://github.com/x/y/pull/53")

    add_candidate(db, "f2", "correction", d(0))
    cluster_one(db, "f2", "env assumed", "k2")
    db.recompute_stats(TODAY, 5)
    db.add_brief(TODAY, pid, 1, "ev", "pat")
    assert "recurred after you accepted it (fix https://github.com/x/y/pull/53, merged)" in report.render(db, TODAY)


# ---- mute verb ------------------------------------------------------------

def test_mute_verb_parses_and_silences(db):
    add_candidate(db, "m1", "correction", d(1))
    cluster_one(db, "m1", "mutable", "k")
    pid = db.find_pattern_by_signature("mutable")["id"]
    db.add_brief(TODAY, pid, 1, "ev", "pat")

    md = report.render(db, TODAY).replace("  note:", "  note: mute", 1)
    verdicts = feedback.parse_journal(md)
    assert any(v.decision == "mute" for v in verdicts)

    feedback.apply(db, verdicts, TODAY, TODAY)
    assert db.get_pattern(pid)["status"] == "muted"
    db.recompute_stats(TODAY, 5)
    assert all(p["id"] != pid for p in db.surface())


# ---- insight -> action translation ---------------------------------------

def test_actions_preview_binds_evidence_to_the_direction(db):
    add_candidate(db, "x1", "error", d(1))     # sets project + an evidence quote
    cluster_one(db, "x1", "write before read", "k")
    pid = db.find_pattern_by_signature("write before read")["id"]
    db.add_brief(TODAY, pid, 1, "ev", "pat")
    db.add_proposal(pid, 1, "Add Read-before-Write to the file-emitting agent")

    out = actions.preview(db, TODAY)
    assert out and "Add Read-before-Write" in out[0]   # the direction
    assert "example-data" in out[0]                    # bound to where it happened
    assert "ev x1" in out[0]                            # and to the proof


def test_actions_queue_lists_only_accepted(db):
    add_candidate(db, "q1", "denial", d(1))
    cluster_one(db, "q1", "perm gate", "k")
    pid = db.find_pattern_by_signature("perm gate")["id"]
    prop_id = db.add_proposal(pid, 1, "Pre-check the lint rule before filing")

    assert actions.queue(db, None) == []               # nothing accepted yet
    db.record_verdict(pid, TODAY, "accept", proposal_id=prop_id)
    db.set_pattern_status(pid, "accepted")
    out = actions.queue(db, None)
    assert out and "Pre-check the lint rule" in out[0]


# ---- ongoing ledger -------------------------------------------------------

def test_ledger_flags_empty_decision_log(db):
    from rem import ledger
    for i in range(3):                                  # >2 runs, no verdicts
        db.start_run(TODAY)
    add_candidate(db, "l1", "correction", d(1))
    cluster_one(db, "l1", "carrying sig", "k")
    pid = db.find_pattern_by_signature("carrying sig")["id"]
    db.bump_streak(pid)                                 # carrying

    txt = ledger.render(db)
    assert "Consolidation ledger" in txt
    assert "Decisions logged: 0" in txt
    assert "0 verdicts" in txt                          # the loud empty-loop flag
    assert "carrying sig" in txt.lower()                # standing pattern listed


def test_save_brief_records_surfaced_count_on_run(db):
    db.start_run(TODAY)
    add_candidate(db, "s1", "correction", d(1))
    cluster_one(db, "s1", "sig", "k")
    pid = db.find_pattern_by_signature("sig")["id"]
    judge.cmd_save_brief(db, {"run_date": TODAY, "items": [
        {"pattern_id": pid, "event": "e", "pattern": "p", "proposals": ["do x"]}]})
    row = db.conn.execute(
        "SELECT patterns_surfaced FROM runs WHERE run_date=?", (TODAY,)).fetchone()
    assert row["patterns_surfaced"] == 1


def test_apply_stamps_decisions_with_the_day_applied(db):
    add_candidate(db, "a1", "error", d(5))
    cluster_one(db, "a1", "late call", "k")
    pid = db.find_pattern_by_signature("late call")["id"]
    db.add_brief("2026-06-10", pid, 1, "ev", "pat")
    md = report.render(db, "2026-06-10").replace("  note:", "  note: drop", 1)
    feedback.apply(db, feedback.parse_journal(md), "2026-06-10", TODAY)
    assert db.get_pattern(pid)["last_verdict_at"] == "2026-06-13"
