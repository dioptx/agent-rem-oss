"""Telegram triage writes the same marks a hand edit would."""

from __future__ import annotations

from rem import feedback, telegram

MD = """# Night consolidation — 2026-09-27

---

## 1 ▼ DRAG · friction · Ran tests in the wrong folder · ×7 (score 24) <!--P:14-->
**Event** — 7 sessions ran tests from the wrong folder.
**Pattern** — Heaviest pattern this week.
**Proposals**
- [ ] T1 — Add a 'check the folder first' line to the rules. <!--p:12-->
- [ ] T2 — Mark settings.json as per-machine. <!--p:13-->
  note:

---

## 2 ▲ LIFT · autonomy · Linked the passing test run · ×5 (score 11) <!--P:9-->
**Event** — 5 sessions ended with a link to the test run.
**Proposals**
- [ ] T1 — Make the test link the closing line. <!--p:10-->
  note:
"""


def test_parse_journal():
    items = telegram.parse_journal(MD)
    assert [i["pattern_id"] for i in items] == [14, 9]
    assert items[0]["valence"] == "drag" and items[1]["valence"] == "lift"
    assert [p["tier"] for p in items[0]["proposals"]] == [1, 2]
    assert items[0]["event"].startswith("7 sessions")


def test_question_labels_have_no_commas_and_carry_extras():
    q = telegram.question(telegram.parse_journal(MD)[0])
    labels = [o["label"] for o in q["options"]]
    assert all("," not in x for x in labels)
    assert labels[-3:] == ["Snooze 7d", "Drop", "Mute"]
    assert q["multiSelect"] is True


def test_accepting_two_tiers_checks_only_that_pattern():
    ans = ", ".join(o["label"] for o in telegram.question(telegram.parse_journal(MD)[0])["options"][:2])
    out = telegram.apply_answer(MD, 14, ans)
    assert "- [x] T1 — Add a 'check the folder first'" in out
    assert "- [x] T2 — Mark settings" in out
    assert "- [ ] T1 — Make the test link" in out          # other pattern untouched


def test_drop_and_free_text_note():
    assert "\n  drop\n" in telegram.apply_answer(MD, 9, "Drop")
    out = telegram.apply_answer(MD, 14, "only on the server, not the laptop")
    assert "  note: only on the server, not the laptop" in out


def test_marks_parse_as_verdicts():
    """The written marks must be what feedback.py reads: accept for pattern 14, drop for 9."""
    md = telegram.apply_answer(MD, 14, telegram.label(1, "x"))
    md = telegram.apply_answer(md, 9, "Drop")
    got = {v.pattern_id: v.decision for v in feedback.parse_journal(md)}
    assert got[14] == "accept" and got[9] == "drop"
