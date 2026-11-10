"""Mechanical predicates: recency weighting + the surface rule.

These encode the recurrence math from the design and must stay exactly right —
they decide what shows up in the morning briefing.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rem import config
from rem.db import ConsolidationDB

TODAY = "2026-06-13"


def d(days_ago: int) -> str:
    return (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()


# ---- recency_weight -------------------------------------------------------

@pytest.mark.parametrize("days_ago,expected", [
    (0, 3),    # today
    (1, 3),    # yesterday — heaviest
    (2, 2),
    (3, 1),
    (5, 1),    # window edge
    (6, 0),    # outside default window
    (-1, 0),   # future / clock skew
])
def test_recency_weight(days_ago, expected):
    assert config.recency_weight(days_ago, window=5) == expected


# ---- fixtures -------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    database = ConsolidationDB(tmp_path / "events.db")
    yield database
    database.close()


def seed_pattern(db, signature, valence="drag", status="open"):
    return db.create_pattern(signature, "convergence", valence, signature.title(), TODAY)


def seed_events(db, pattern_id, days_ago_list, valence="drag"):
    for i, da in enumerate(days_ago_list):
        db.insert_event({
            "id": f"{pattern_id}-{i}-{da}",
            "run_date": TODAY,
            "occurred_date": d(da),
            "kind": "correction",
            "valence": valence,
            "axis": "convergence",
            "signature": f"sig{pattern_id}",
            "title": "x",
            "pattern_id": pattern_id,
        })


# ---- recompute_stats ------------------------------------------------------

def test_recompute_counts_and_weights(db):
    p = seed_pattern(db, "naming")
    # one yesterday (w3), one 2d (w2), one 3d (w1), one 9d (out of window, w0)
    seed_events(db, p, [1, 2, 3, 9])          # seeded as kind="correction"
    db.recompute_stats(TODAY, window=5)
    row = db.get_pattern(p)
    assert row["occurrence_count"] == 3          # the 9-day-old one drops out
    # recency (3+2+1) discounted by the correction severity factor
    sev = config.kind_severity("correction")
    assert row["weighted_score"] == round((3 + 2 + 1) * sev)
    assert row["last_seen"] == d(1)


# ---- surface rule ---------------------------------------------------------

def test_surface_orders_qualifiers_by_score(db):
    a = seed_pattern(db, "alpha")
    b = seed_pattern(db, "beta")
    seed_events(db, a, [1, 1, 1])   # score 9, count 3
    seed_events(db, b, [1, 2, 3])   # score 6, count 3
    db.recompute_stats(TODAY)
    out = db.surface()
    assert [p["signature"] for p in out] == ["alpha", "beta"]


def test_surface_caps_at_limit(db):
    for i in range(8):
        p = seed_pattern(db, f"p{i}")
        seed_events(db, p, [1, 1, 1])
    db.recompute_stats(TODAY)
    assert len(db.surface(limit=5)) == 5


def test_surface_backfills_when_few_qualify(db):
    # only one pattern clears >=3; two others have a single recent event each
    a = seed_pattern(db, "qualifier")
    b = seed_pattern(db, "thin1")
    c = seed_pattern(db, "thin2")
    seed_events(db, a, [1, 1, 2])   # count 3 -> qualifies
    seed_events(db, b, [1])         # count 1
    seed_events(db, c, [2])         # count 1
    db.recompute_stats(TODAY)
    out = db.surface()
    # never empty: backfills up to 3 even though only one cleared the bar
    assert len(out) == 3
    assert out[0]["signature"] == "qualifier"


def test_surface_excludes_zero_weight(db):
    a = seed_pattern(db, "stale")
    seed_events(db, a, [9, 10])     # all outside window -> score 0
    db.recompute_stats(TODAY, window=5)
    assert db.surface() == []


def test_surface_excludes_dropped(db):
    a = seed_pattern(db, "dropped")
    seed_events(db, a, [1, 1, 1])
    db.recompute_stats(TODAY)
    db.set_pattern_status(a, "dropped")
    assert db.surface() == []


def test_escalation_floats_neglected_pattern(db):
    a = seed_pattern(db, "fresh")
    b = seed_pattern(db, "neglected")
    seed_events(db, a, [1, 1, 1, 1])   # score 12, streak 0
    seed_events(db, b, [1, 2, 3])      # score 6
    db.recompute_stats(TODAY)
    assert [p["signature"] for p in db.surface()][:2] == ["fresh", "neglected"]
    for _ in range(6):                 # ignored across 6 briefings
        db.bump_streak(b)
    db.recompute_stats(TODAY)
    assert db.surface()[0]["signature"] == "neglected"   # escalation wins


def test_mark_resolved_clears_streak(db):
    a = seed_pattern(db, "x")
    seed_events(db, a, [1])
    for _ in range(3):
        db.bump_streak(a)
    db.mark_resolved(a, TODAY)
    assert db.get_pattern(a)["surfaced_streak"] == 0
    assert db.get_pattern(a)["last_verdict_at"] == TODAY


def test_surface_swaps_in_a_lift(db):
    # three strong drags + one weaker lift; lift should displace the weakest drag
    for i in range(3):
        p = seed_pattern(db, f"drag{i}")
        seed_events(db, p, [1, 1, 1])
    lift = seed_pattern(db, "win", valence="lift")
    seed_events(db, lift, [3], valence="lift")   # low score
    db.recompute_stats(TODAY)
    out = db.surface(limit=3)
    sigs = [p["signature"] for p in out]
    assert "win" in sigs
    assert len(out) == 3
