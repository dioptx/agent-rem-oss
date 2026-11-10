"""End-to-end: cluster -> targets -> save-brief -> render -> parse feedback.

Exercises the JSON contracts between the deterministic layer and the LLM step,
and the hidden-marker round-trip that lets a checkmark become a verdict.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rem import feedback, judge, report
from rem.db import ConsolidationDB

TODAY = "2026-06-13"


def d(days_ago):
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def db(tmp_path):
    database = ConsolidationDB(tmp_path / "events.db")
    yield database
    database.close()


def add_candidate(db, eid, kind, day, project="example-data"):
    db.insert_event({
        "id": eid, "run_date": TODAY, "occurred_date": day,
        "session_id": eid, "project": project, "kind": kind,
        "title": f"{kind} ({project})", "detail": f"1x in {eid}",
        "evidence": [f"evidence for {eid}"], "raw_weight": 1,
    })


def seed_recurring(db):
    # three "naming" occurrences over three days -> clears the bar
    for i, da in enumerate([1, 1, 2]):
        add_candidate(db, f"nm{i}", "correction", d(da))
    # one "win" occurrence (a lift)
    add_candidate(db, "win0", "learning", d(1))


def cluster_all(db):
    payload = {
        "events": [
            {"id": "nm0", "axis": "convergence", "valence": "drag",
             "signature": "example-data naming", "pattern_key": "naming"},
            {"id": "nm1", "axis": "convergence", "valence": "drag",
             "signature": "example-data naming", "pattern_key": "naming"},
            {"id": "nm2", "axis": "convergence", "valence": "drag",
             "signature": "example-data naming", "pattern_key": "naming"},
            {"id": "win0", "axis": "autonomy", "valence": "lift",
             "signature": "solo regression queries", "pattern_key": "win"},
        ],
        "patterns": {
            "naming": {"signature": "example-data naming", "axis": "convergence",
                       "valence": "drag", "title": "Re-explaining naming"},
            "win": {"signature": "solo regression queries", "axis": "autonomy",
                    "valence": "lift", "title": "Bounded regression solo"},
        },
    }
    return judge.cmd_cluster(db, TODAY, payload)


def test_cluster_creates_patterns_and_assigns(db):
    seed_recurring(db)
    res = cluster_all(db)
    assert res["assigned"] == 4
    assert res["patterns_created"] == 2
    assert db.candidates() == []   # all now tagged


def test_reextract_preserves_tags(db):
    # tag + cluster an event, then re-extract the same id -> tags must survive
    add_candidate(db, "e1", "correction", d(1))
    pid = db.create_pattern("sig", "convergence", "drag", "T", TODAY)
    db.tag_event("e1", "convergence", "drag", "sig")
    db.assign_event("e1", pid)
    db.insert_event({"id": "e1", "run_date": TODAY, "occurred_date": d(1),
                     "kind": "correction", "title": "x"})   # re-extraction
    row = db.conn.execute(
        "SELECT signature, pattern_id FROM events WHERE id='e1'").fetchone()
    assert row["signature"] == "sig" and row["pattern_id"] == pid
    assert db.candidates() == []   # already judged, not re-offered


def test_targets_surface_recurring_pattern(db):
    seed_recurring(db)
    cluster_all(db)
    targets = judge.cmd_targets(db, TODAY, window=5)
    sigs = {t["signature"] for t in targets}
    assert "example-data naming" in sigs
    naming = next(t for t in targets if t["signature"] == "example-data naming")
    assert naming["occurrence_count"] == 3
    assert naming["evidence"]            # carried through for proposal-writing


def save_brief(db, targets):
    items = []
    for t in targets:
        items.append({
            "pattern_id": t["pattern_id"],
            "event": f"{t['title']} seen {t['occurrence_count']}x",
            "pattern": "recurring; matters",
            "proposals": [f"T1 for {t['signature']}", f"T2 for {t['signature']}"],
        })
    return judge.cmd_save_brief(db, {"run_date": TODAY, "items": items})


def test_report_has_layers_and_markers(db):
    seed_recurring(db)
    cluster_all(db)
    save_brief(db, judge.cmd_targets(db, TODAY, 5))
    md = report.render(db, TODAY)
    assert "**Event**" in md and "**Pattern**" in md and "**Proposals**" in md
    assert "<!--P:" in md and "<!--p:" in md
    assert "▼ DRAG" in md and "▲ LIFT" in md


def test_checkmark_becomes_accept_verdict(db):
    seed_recurring(db)
    cluster_all(db)
    save_brief(db, judge.cmd_targets(db, TODAY, 5))
    md = report.render(db, TODAY)

    # human checks the first proposal of the first item only
    checked = md.replace("- [ ]", "- [x]", 1)
    verdicts = feedback.parse_journal(checked)
    accepted = [v for v in verdicts if v.decision == "accept"]
    carried = [v for v in verdicts if v.decision == "carry"]
    assert len(accepted) == 1
    assert carried                       # untouched items carry over, never drop


def test_legend_snooze_not_parsed_as_verdict(db):
    seed_recurring(db)
    cluster_all(db)
    save_brief(db, judge.cmd_targets(db, TODAY, 5))
    md = report.render(db, TODAY)
    # the legend mentions `snooze Nd` and `note:` inside a blockquote
    for v in feedback.parse_journal(md):
        assert v.snooze_days is None


def test_blank_carries_and_bumps_streak(db):
    seed_recurring(db)
    cluster_all(db)
    save_brief(db, judge.cmd_targets(db, TODAY, 5))
    md = report.render(db, TODAY)

    verdicts = feedback.parse_journal(md)       # nothing touched
    assert all(v.decision == "carry" for v in verdicts)
    feedback.apply(db, verdicts, TODAY, TODAY, bump_carries=True)

    db.recompute_stats(TODAY, 5)
    surfaced = db.surface()
    assert surfaced                              # carried items still surface
    assert all((db.get_pattern(v.pattern_id)["surfaced_streak"]) == 1
               for v in verdicts)               # and their streak climbed


def test_review_does_not_bump_streak(db):
    seed_recurring(db)
    cluster_all(db)
    save_brief(db, judge.cmd_targets(db, TODAY, 5))
    md = report.render(db, TODAY)
    feedback.apply(db, feedback.parse_journal(md), TODAY, TODAY, bump_carries=False)
    pid = feedback.parse_journal(md)[0].pattern_id
    assert db.get_pattern(pid)["surfaced_streak"] == 0


def test_explicit_drop_stops_surfacing(db):
    seed_recurring(db)
    cluster_all(db)
    save_brief(db, judge.cmd_targets(db, TODAY, 5))
    md = report.render(db, TODAY)

    md2 = md.replace("  note:", "  note: drop")   # explicit drop on every item
    verdicts = feedback.parse_journal(md2)
    assert all(v.decision == "drop" for v in verdicts)
    feedback.apply(db, verdicts, TODAY, TODAY)

    db.recompute_stats(TODAY, 5)
    assert db.surface() == []                     # dropped patterns gone


def test_process_pending_is_idempotent(db, tmp_path):
    pid = db.create_pattern("sig", "friction", "drag", "T", d(1))
    jd = tmp_path / "j"
    jd.mkdir()
    (jd / f"journal-{d(1)}.md").write_text(
        f"## 1 ▼ DRAG · friction · T · ×1 (score 1) <!--P:{pid}-->\n"
        f"**Event** — x\n**Pattern** — y\n**Proposals**\n"
        f"- [ ] T1 — z <!--p:1-->\n  note:\n", encoding="utf-8")
    r1 = feedback.process_pending(db, TODAY, jd)
    assert r1["journals"] == 1 and r1["carry"] == 1
    assert db.get_pattern(pid)["surfaced_streak"] == 1
    r2 = feedback.process_pending(db, TODAY, jd)     # already consolidated
    assert r2["journals"] == 0
    assert db.get_pattern(pid)["surfaced_streak"] == 1   # not double-counted


def test_snooze_directive_sets_status(db):
    seed_recurring(db)
    cluster_all(db)
    save_brief(db, judge.cmd_targets(db, TODAY, 5))
    md = report.render(db, TODAY)
    # append a snooze directive to the first item's note line
    md2 = md.replace("  note:", "  note: snooze 7d", 1)
    verdicts = feedback.parse_journal(md2)
    feedback.apply(db, verdicts, TODAY, TODAY)
    snoozed = [p for p in [db.get_pattern(v.pattern_id) for v in verdicts]
               if p["status"] == "snoozed"]
    assert snoozed
    assert snoozed[0]["snoozed_until"] == (
        date.fromisoformat(TODAY) + timedelta(days=7)).isoformat()
