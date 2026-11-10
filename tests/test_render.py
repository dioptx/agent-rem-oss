"""Smoke tests for both rendered surfaces: badges, gap header, valid HTML."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from rem import html_report, report
from rem.db import ConsolidationDB

TODAY = "2026-06-14"


def d(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()


@pytest.fixture
def seeded(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    pid = db.create_pattern("naming", "convergence", "drag", "Naming", d(1))
    for i, da in enumerate([1, 1, 2]):
        db.insert_event({"id": f"e{i}", "run_date": TODAY, "occurred_date": d(da),
                         "session_id": f"s{i}", "project": "example-data",
                         "kind": "correction", "valence": "drag", "axis": "convergence",
                         "signature": "naming", "title": "Naming", "pattern_id": pid})
    db.conn.execute("UPDATE patterns SET surfaced_streak=4 WHERE id=?", (pid,))
    db.add_brief(TODAY, pid, 1, "corrected naming 6x", "your #1 recurring correction")
    db.add_proposal(pid, 1, "pin it in AGENTS.md")
    db.record_verdict(999, d(3), "accept")     # establishes a prior review date
    db.recompute_stats(TODAY, 5)
    yield db
    db.close()


def test_markdown_has_badge_and_gap(seeded):
    md = report.render(seeded, TODAY)
    assert "[Day 5 · unresolved]" in md          # streak 4 -> Day 5
    assert "3d since last review" in md
    assert "carries over" in md                   # new legend semantics


def test_html_is_self_contained_and_marked(seeded):
    h = html_report.render_html(seeded, TODAY)
    assert h.startswith("<!doctype html>")
    assert "<style>" in h and "</html>" in h      # inline CSS, no external deps
    assert "Day 5 · unresolved" in h
    assert "since last review" in h
    assert "timeline" in h                        # the wake-> visual
