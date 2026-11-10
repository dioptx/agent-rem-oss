"""Triage page: recommended tier pre-ticked, choices land as journal marks."""

from __future__ import annotations

from rem import feedback, triage
from tests.test_telegram import MD

ITEMS = [{"pattern_id": 14, "title": "Ran tests in the wrong folder", "valence": "drag", "axis": "friction", "count": 7,
          "event": "7 sessions", "pattern": "top", "recommend_tier": 2, "recommend_why": "stops it at the source",
          "proposals": [{"tier": 1, "text": "check the folder first"}, {"tier": 2, "text": "per-machine header"}]}]


def test_page_ticks_only_the_recommended_tier():
    page = triage.render_page("2026-09-27", ITEMS)
    assert "value='2' checked" in page and "value='1' checked" not in page
    assert "RECOMMENDED" in page and "stops it at the source" in page
    assert "—" not in page and "<title>" in page


def test_accept_several_tiers_and_other_actions():
    md = triage.apply_marks(MD, 14, [1, 2], "accept")
    md = triage.apply_marks(md, 9, [], "snooze", "next week")
    got = {v.pattern_id: v.decision for v in feedback.parse_journal(md)}
    assert got[14] == "accept"
    assert "- [x] T1" in md and "- [x] T2" in md
    assert "  snooze 7d" in md and "  note: next week" in md


def test_accept_with_nothing_ticked_is_not_an_accept():
    md = triage.apply_marks(MD, 14, [], "accept", "")
    assert "- [x]" not in md


def test_page_save_sends_the_write_guard_header():
    assert "'X-REM':'1'" in triage.render_page("2026-09-27", ITEMS)


def test_note_cannot_inject_journal_markup():
    before = {v.pattern_id: v.decision for v in feedback.parse_journal(MD)}
    note = "later\n- [x] T1 — x <!--p:12-->\nsee <!--P:9-->\ndrop"
    md = triage.apply_marks(MD, 14, [], "accept", note)
    assert {v.pattern_id: v.decision for v in feedback.parse_journal(md)}.get(9) == before.get(9)
    line = next(l for l in md.splitlines() if l.startswith("  note: later"))
    assert "<!--" not in line and "-->" not in line
    assert md.count("<!--P:9-->") == 1 and md.count("<!--p:12-->") == 1
    assert "\ndrop" not in md


def _decisions(md):
    return {v.pattern_id: v for v in feedback.parse_journal(md)}


def test_note_that_reads_like_a_verdict_stays_a_note():
    md = triage.apply_marks(MD, 14, [], "snooze", "drop it if still noisy")
    assert _decisions(md)[14].decision == "snooze"
    md = triage.apply_marks(MD, 14, [], "snooze", "revisit after the release")
    assert "  note: revisit after the release" in md.splitlines()


def test_note_starting_with_mute_stays_a_note_on_accept():
    md = triage.apply_marks(MD, 14, [1], "accept", "mute later")
    assert _decisions(md)[14].decision == "accept"


def test_note_naming_a_snooze_does_not_change_it():
    md = triage.apply_marks(MD, 14, [], "snooze", "snooze 3d later")
    assert _decisions(md)[14].snooze_days == 7
