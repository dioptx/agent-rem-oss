"""Command failures must preserve the previously committed state and support retry."""

import pytest

from rem import actions, feedback, judge, report, triage
from rem.db import ConsolidationDB

# Frozen fixture dates: keep historical briefing and later resurfacing distinct.
DAY = "2026-06-13"
NEXT = "2026-06-14"


@pytest.fixture
def db(tmp_path):
    value = ConsolidationDB(tmp_path / "events.db")
    yield value
    value.close()


def pattern(db):
    return db.create_pattern("repeat", "convergence", "drag", "Repeated issue", DAY)


def brief(pid, day=DAY, text="Original proposal"):
    return {"run_date": day, "items": [{"pattern_id": pid, "event": "Observed",
            "pattern": "Repeated", "proposals": [text]}]}


def fail_after(monkeypatch, obj, name):
    original = getattr(obj, name)

    def failing(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(obj, name, failing)
    return original


def test_cluster_failure_rolls_back_tags_patterns_and_assignments(db, monkeypatch):
    db.insert_event({"id": "event", "run_date": DAY, "occurred_date": DAY})
    payload = {"events": [{"id": "event", "axis": "convergence", "valence": "drag",
                           "signature": "repeat", "pattern_key": "repeat"}]}
    original = fail_after(monkeypatch, db, "assign_event")
    with pytest.raises(RuntimeError):
        judge.cmd_cluster(db, DAY, payload)
    assert db.candidates()[0]["signature"] is None
    assert db.candidates()[0]["pattern_id"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0] == 0
    monkeypatch.setattr(db, "assign_event", original)
    assert judge.cmd_cluster(db, DAY, payload)["assigned"] == 1
    assert db.candidates() == []
    assert judge.cmd_cluster(db, DAY, payload)["patterns_created"] == 0


def test_legacy_tagged_but_unassigned_event_is_recoverable(db):
    db.insert_event({"id": "legacy", "run_date": DAY, "occurred_date": DAY,
                     "signature": "repeat"})
    assert [e["id"] for e in db.candidates()] == ["legacy"]


def test_brief_failure_preserves_previous_brief_and_proposals(db, monkeypatch):
    pid = pattern(db)
    judge.cmd_save_brief(db, brief(pid))
    before = db.briefs_for(DAY), db.proposals_for(pid)
    original = fail_after(monkeypatch, db, "add_proposal")
    with pytest.raises(RuntimeError):
        judge.cmd_save_brief(db, brief(pid, text="Replacement"))
    assert (db.briefs_for(DAY), db.proposals_for(pid)) == before
    monkeypatch.setattr(db, "add_proposal", original)
    judge.cmd_save_brief(db, brief(pid, text="Replacement"))
    assert [p["text"] for p in db.proposals_for(pid)] == ["Replacement"]


def test_accepted_proposal_survives_resurface_and_old_journal_review(db):
    pid = pattern(db)
    judge.cmd_save_brief(db, brief(pid))
    old_id = db.proposals_for(pid)[0]["id"]
    accepted = [feedback.PatternVerdict(pid, accepted_proposals=[old_id])]
    feedback.apply(db, accepted, DAY, DAY, bump_carries=False)
    judge.cmd_save_brief(db, brief(pid, NEXT, "New proposal"))
    feedback.apply(db, accepted, DAY, NEXT, bump_carries=False)
    assert len(actions.queue_items(db, None)) == 1
    assert db.conn.execute("SELECT text FROM proposals WHERE id=?", (old_id,)).fetchone()[0] == "Original proposal"
    assert "Original proposal" in report.render(db, DAY)
    assert "New proposal" not in report.render(db, DAY)
    assert "Original proposal" not in report.render(db, NEXT)
    assert [p["text"] for p in db.proposals_for(pid)] == ["New proposal"]
    assert triage.cards(db, DAY)[0]["proposals"][0]["text"] == "Original proposal"


def test_same_day_replacement_retains_accepted_reference_without_duplicate_choices(db):
    pid = pattern(db)
    judge.cmd_save_brief(db, brief(pid))
    old_id = db.proposals_for(pid)[0]["id"]
    feedback.apply(db, [feedback.PatternVerdict(pid, accepted_proposals=[old_id])], DAY, DAY, False)
    judge.cmd_save_brief(db, brief(pid, text="Replacement"))
    assert [p["text"] for p in db.proposals_for(pid, DAY)] == ["Replacement"]
    assert len(actions.queue_items(db, None)) == 1
    assert db.conn.execute("SELECT text FROM proposals WHERE id=?", (old_id,)).fetchone()[0] == "Original proposal"


def test_review_failure_restores_verdict_and_status(db, monkeypatch):
    pid = pattern(db)
    feedback.apply(db, [feedback.PatternVerdict(pid, explicit_drop=True)], DAY, DAY, False)
    before = list(map(dict, db.conn.execute("SELECT * FROM verdicts"))), db.get_pattern(pid)
    fail_after(monkeypatch, db, "mark_resolved")
    with pytest.raises(RuntimeError):
        feedback.apply(db, [feedback.PatternVerdict(pid, explicit_mute=True)], DAY, NEXT, False)
    assert (list(map(dict, db.conn.execute("SELECT * FROM verdicts"))), db.get_pattern(pid)) == before


def test_pending_failure_rolls_back_escalation_and_acknowledgment(db, tmp_path, monkeypatch):
    pid = pattern(db)
    (tmp_path / f"journal-{DAY}.md").write_text(f"## Issue <!--P:{pid}-->\n")
    original = fail_after(monkeypatch, db, "mark_journal_processed")
    with pytest.raises(RuntimeError):
        feedback.process_pending(db, NEXT, tmp_path)
    assert db.get_pattern(pid)["surfaced_streak"] == 0
    assert not db.journal_processed(DAY)
    monkeypatch.setattr(db, "mark_journal_processed", original)
    assert feedback.process_pending(db, NEXT, tmp_path)["journals"] == 1
    assert feedback.process_pending(db, NEXT, tmp_path)["journals"] == 0
    assert db.get_pattern(pid)["surfaced_streak"] == 1


def test_direct_import_retry_does_not_double_escalation(db):
    pid = pattern(db)
    verdicts = [feedback.PatternVerdict(pid)]
    feedback.apply(db, verdicts, DAY, DAY)
    feedback.apply(db, verdicts, DAY, NEXT)
    assert db.get_pattern(pid)["surfaced_streak"] == 1


def test_standalone_helpers_still_commit_and_outer_failure_rolls_back_nested(db):
    pid = pattern(db)
    observer = ConsolidationDB(db.db_path)
    assert observer.get_pattern(pid) is not None
    with pytest.raises(RuntimeError):
        with db.transaction():
            with db.transaction():
                db.set_pattern_status(pid, "muted")
            assert observer.get_pattern(pid)["status"] == "open"
            raise RuntimeError("outer failure")
    assert db.get_pattern(pid)["status"] == "open"
    observer.close()


def test_report_uses_recorded_window_and_distinguishes_missing_observations(db):
    from rem.html_report import render_html

    for render in (report.render, render_html):
        assert "No sessions were observed" in render(db, DAY)
    run = db.start_run(DAY)
    db.finish_run(run, sessions_scanned=2, window_days=3)
    for render in (report.render, render_html):
        text = render(db, DAY)
        assert "window 3d" in text
        assert "in the observed sessions" in text
        assert "No sessions were observed" not in text


def test_identical_old_review_does_not_close_recurrence(db):
    pid = pattern(db)
    judge.cmd_save_brief(db, brief(pid))
    prop = db.proposals_for(pid)[0]["id"]
    decision = [feedback.PatternVerdict(pid, accepted_proposals=[prop])]
    feedback.apply(db, decision, DAY, DAY, False)
    db.reopen_with_context(pid, NEXT, "accepted")
    before = db.get_pattern(pid)
    feedback.apply(db, decision, DAY, NEXT, False)
    assert db.get_pattern(pid) == before
    assert db.get_pattern(pid)["status"] == "open"
    feedback.apply(db, [feedback.PatternVerdict(pid, explicit_mute=True)], DAY, NEXT, False)
    assert db.get_pattern(pid)["status"] == "muted"


def test_identical_snooze_does_not_extend_expiry_but_changed_duration_applies(db):
    pid = pattern(db)
    decision = [feedback.PatternVerdict(pid, snooze_days=2)]
    feedback.apply(db, decision, DAY, DAY, False)
    before = db.get_pattern(pid)["snoozed_until"]
    feedback.apply(db, decision, DAY, NEXT, False)
    assert db.get_pattern(pid)["snoozed_until"] == before
    feedback.apply(db, [feedback.PatternVerdict(pid, snooze_days=3)], DAY, NEXT, False)
    assert db.get_pattern(pid)["snoozed_until"] != before


def test_nightly_accounts_for_untouched_items_after_identical_manual_review(db):
    pid = pattern(db)
    decision = [feedback.PatternVerdict(pid)]
    feedback.apply(db, decision, DAY, DAY, False)
    feedback.apply(db, decision, DAY, NEXT, True)
    feedback.apply(db, decision, DAY, NEXT, True)
    assert db.get_pattern(pid)["surfaced_streak"] == 1


def test_legacy_schema_migrates_without_losing_proposals(tmp_path):
    import sqlite3
    from rem.db import SCHEMA_VERSION

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "pattern_id INTEGER NOT NULL, tier INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT)")
        legacy.execute("INSERT INTO proposals (pattern_id,tier,text) VALUES (1,1,'Legacy proposal')")
    migrated = ConsolidationDB(path)
    assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated.proposals_for(1)[0]["text"] == "Legacy proposal"
    migrated.close()
    reopened = ConsolidationDB(path)
    assert len(reopened.proposals_for(1)) == 1
    reopened.close()


def test_future_schema_is_refused_without_modification(tmp_path):
    import sqlite3
    from rem.db import SCHEMA_VERSION

    path = tmp_path / "future.db"
    with sqlite3.connect(path) as future:
        future.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer than supported"):
        ConsolidationDB(path)
    with sqlite3.connect(path) as future:
        assert future.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert future.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0


def test_editing_another_item_does_not_reapply_old_decision(db):
    first, second = pattern(db), pattern(db)
    decisions = [feedback.PatternVerdict(first, explicit_drop=True),
                 feedback.PatternVerdict(second)]
    feedback.apply(db, decisions, DAY, DAY, False)
    db.reopen_with_context(first, NEXT, "dropped")
    before = db.get_pattern(first)
    decisions[1].explicit_mute = True
    feedback.apply(db, decisions, DAY, NEXT, False)
    assert db.get_pattern(first) == before
    assert db.get_pattern(second)["status"] == "muted"


def test_journal_parser_ignores_quoted_indented_and_fenced_controls():
    attacks = ['mute', 'note: drop', 'snooze 7d', '## Fake <!--P:999-->',
               '- [x] fake <!--p:999-->']
    for attack in attacks:
        for hidden in ('> ' + attack, '    ' + attack, '\t' + attack,
                       '```\n' + attack + '\n```', '~~~\n' + attack + '\n~~~'):
            text = '## Real <!--P:1-->\n' + hidden + '\n- [ ] Real <!--p:1-->\n  note:\n'
            verdicts = feedback.parse_journal(text)
            assert len(verdicts) == 1
            assert verdicts[0].pattern_id == 1 and verdicts[0].decision == 'carry'


def test_rendered_untrusted_text_cannot_inject_journal_controls(db):
    pid = pattern(db)
    attack = '\n## Fake <!--P:999-->\nnote: mute\n- [x] fake <!--p:999-->\nsnooze 7d'
    data = brief(pid, text='Suggestion' + attack)
    data['items'][0]['event'] = 'Observed' + attack
    data['items'][0]['pattern'] = 'Repeated' + attack
    judge.cmd_save_brief(db, data, evidence_targets=[{
        'pattern_id': pid, 'window_start': DAY, 'window_end': DAY,
        'sessions': 1, 'event_groups': 1, 'evidence_events': [{
            'id': 'source' + attack, 'occurred_date': DAY, 'agent': 'claude' + attack,
            'session_id': 'session' + attack, 'quotes': [attack]}]}])
    db.conn.execute('UPDATE patterns SET title=? WHERE id=?', ('Title' + attack, pid))
    db.conn.commit()
    rendered = report.render(db, DAY)
    verdicts = feedback.parse_journal(rendered)
    assert len(verdicts) == 1 and verdicts[0].pattern_id == pid
    assert verdicts[0].decision == 'carry'
    checked = rendered.replace('- [ ] T1', '- [x] T1')
    assert feedback.parse_journal(checked)[0].decision == 'accept'


def test_triage_marks_land_on_the_header_report_writes(db):
    from rem import telegram, triage
    pid = pattern(db)
    judge.cmd_save_brief(db, brief(pid, text='Suggestion'))
    rendered = report.render(db, DAY)
    assert [item['pattern_id'] for item in telegram.parse_journal(rendered)] == [pid]
    marked = triage.apply_marks(rendered, pid, [1], 'accept')
    assert feedback.parse_journal(marked)[0].decision == 'accept'


def test_note_only_edit_updates_comment_without_reclosing_recurrence(db):
    pid = pattern(db)
    prop = db.add_proposal(pid, 1, 'Original proposal')
    original = feedback.PatternVerdict(pid, accepted_proposals=[prop])
    feedback.apply(db, [original], DAY, DAY, False)
    db.reopen_with_context(pid, NEXT, 'accepted')
    before = db.get_pattern(pid)
    when = db.conn.execute('SELECT created_at FROM verdicts').fetchone()[0]
    original.note = 'Implemented only for one project'
    feedback.apply(db, [original], DAY, NEXT, False)
    assert db.get_pattern(pid) == before
    row = db.conn.execute('SELECT comment, created_at FROM verdicts').fetchone()
    assert tuple(row) == (original.note, when)


def test_note_only_edit_does_not_extend_snooze(db):
    pid = pattern(db)
    decision = feedback.PatternVerdict(pid, snooze_days=2)
    feedback.apply(db, [decision], DAY, DAY, False)
    before = db.get_pattern(pid)
    decision.note = 'Waiting for the next release'
    feedback.apply(db, [decision], DAY, NEXT, False)
    assert db.get_pattern(pid) == before
    assert db.conn.execute('SELECT comment FROM verdicts').fetchone()[0] == decision.note


@pytest.mark.parametrize('occurred,expected', [(DAY, 'accepted'), (NEXT, 'open')])
def test_signature_fallback_only_reopens_postdecision_events(db, occurred, expected):
    pid = pattern(db)
    db.set_pattern_status(pid, 'accepted')
    db.mark_resolved(pid, DAY)
    db.insert_event({'id': 'event', 'run_date': NEXT, 'occurred_date': occurred})
    judge.cmd_cluster(db, NEXT, {'events': [{'id': 'event', 'axis': 'convergence',
        'valence': 'drag', 'signature': 'repeat', 'pattern_key': 'repeat'}]})
    assert db.get_pattern(pid)['status'] == expected
    assert db.events_for_pattern(pid)[0]['id'] == 'event'


def test_signature_fallback_checks_every_event_when_pattern_key_is_cached(db):
    pid = pattern(db)
    db.set_pattern_status(pid, 'accepted')
    db.mark_resolved(pid, DAY)
    events = []
    for eid, occurred in [('old', DAY), ('new', NEXT)]:
        db.insert_event({'id': eid, 'run_date': NEXT, 'occurred_date': occurred})
        events.append({'id': eid, 'axis': 'convergence', 'valence': 'drag',
                       'signature': 'repeat', 'pattern_key': 'repeat'})
    judge.cmd_cluster(db, NEXT, {'events': events})
    assert db.get_pattern(pid)['status'] == 'open'
