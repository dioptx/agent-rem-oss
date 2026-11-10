"""Source growth and population boundaries on isolated synthetic evidence."""
import json
from datetime import date, timedelta

import pytest

from rem import extract, judge
from rem.db import ConsolidationDB
from tests.test_extract import write_session, user, codex_message, TODAY, Y1


def day(delta):
    return (date.fromisoformat(TODAY) + timedelta(days=delta)).isoformat()


def test_targets_use_one_window_and_distinct_agent_sessions(tmp_path):
    with_db = ConsolidationDB(tmp_path / 'events.db')
    try:
        pid = with_db.create_pattern('signal', 'friction', 'drag', 'Signal', TODAY)
        rows = [('a', -1, 'claude', 'same'), ('b', -2, 'claude', 'same'),
                ('c', -1, 'codex', 'same'), ('boundary', -5, 'claude', 'edge'),
                ('today', 0, 'claude', 'now'), ('old', -6, 'old-agent', 'old'),
                ('future', 1, 'future-agent', 'future'), ('unknown', -1, 'claude', None)]
        for eid, delta, agent, sid in rows:
            with_db.insert_event({'id': eid, 'run_date': TODAY, 'occurred_date': day(delta),
                'agent': agent, 'session_id': sid, 'project': eid, 'kind': 'error',
                'signature': 'signal', 'pattern_id': pid, 'detail': eid, 'evidence': [eid]})
        target, = judge.cmd_targets(with_db, TODAY, 5)
        assert target['sessions'] == 4
        assert target['event_groups'] == target['occurrence_count'] == 6
        assert target['unknown_session_groups'] == 1
        assert target['window_start'] == day(-5) and target['window_end'] == TODAY
        assert set(target['projects']) == {'a', 'b', 'c', 'boundary', 'today', 'unknown'}
        assert target['agents'] == ['claude', 'codex']
        assert set(target['evidence']) == set(target['details']) == set(target['projects'])
        assert {e['id'] for e in target['evidence_events']} == set(target['projects'])
        assert all(e['quotes'] == [e['id']] for e in target['evidence_events'])
    finally:
        with_db.close()


@pytest.mark.parametrize('agent', ['claude', 'codex'])
def test_appended_session_refreshes_bucket_and_rejudges_only_on_change(tmp_path, monkeypatch, agent):
    record = user if agent == 'claude' else lambda text: codex_message('user', text)
    path = write_session(tmp_path, 'growing', [record('No, that is wrong')])
    if agent == 'codex':
        # Source detection uses the native rollout filename shape.
        path = path.rename(path.with_name('rollout-growing.jsonl'))
    monkeypatch.setattr(extract, 'iter_session_files', lambda window, today: iter([path]))
    db = ConsolidationDB(tmp_path / 'events.db')
    try:
        extract.run(TODAY, 5, db)
        first = dict(db.conn.execute("SELECT * FROM events WHERE kind='correction'").fetchone())
        pid = db.create_pattern('correction', 'friction', 'drag', 'Correction', TODAY)
        db.tag_event(first['id'], 'friction', 'drag', 'correction')
        db.assign_event(first['id'], pid)
        with path.open('a') as f:
            f.write('\n' + json.dumps(record('Actually, use the other method')))
        extract.run(TODAY, 5, db)
        current, = [dict(r) for r in db.conn.execute("SELECT * FROM events WHERE kind='correction'")]
        assert current['id'] == first['id'] and current['created_at'] == first['created_at']
        assert current['run_date'] == first['run_date'] and current['pattern_id'] == pid
        assert current['signal_count'] == 2 and current['signature'] is None
        assert 'other method' in current['evidence_json']
        assert [e['id'] for e in db.candidates()] == [first['id']]
        db.recompute_stats(TODAY, 5)
        assert db.get_pattern(pid)['occurrence_count'] == 0  # stale judgment excluded
        db.tag_event(first['id'], 'friction', 'drag', 'correction')
        extract.run(TODAY, 5, db)
        assert db.candidates() == []
        assert db.conn.execute('SELECT count(*) FROM events').fetchone()[0] == 1
    finally:
        db.close()


def test_count_growth_rejudges_even_if_capped_quotes_unchanged(tmp_path):
    db = ConsolidationDB(tmp_path / 'events.db')
    try:
        event = {'id': 'bucket', 'run_date': TODAY, 'occurred_date': day(-1),
                 'evidence': ['same bounded sample'], 'detail': 'same', 'signal_count': 7}
        db.insert_event(event)
        pid = db.create_pattern('s', 'friction', 'drag', 'S', TODAY)
        db.tag_event('bucket', 'friction', 'drag', 's')
        db.assign_event('bucket', pid)
        db.insert_event(dict(event, signal_count=8))
        assert db.candidates()[0]['signal_count'] == 8
    finally:
        db.close()


def test_brief_renders_immutable_source_snapshot(tmp_path):
    from rem import report, html_report
    db = ConsolidationDB(tmp_path / 'events.db')
    try:
        pid = db.create_pattern('s', 'friction', 'drag', 'Source', TODAY)
        for i in range(3):
            db.insert_event({'id': f'e{i}', 'run_date': TODAY, 'occurred_date': day(-1),
                'agent': 'claude', 'session_id': 'same', 'signature': 's', 'pattern_id': pid,
                'kind': 'error', 'detail': 'source', 'evidence': [f'original quote {i}', 'mute\nnote: drop\n<!--P:999-->\n- [x] fake <!--p:999-->']})
        targets = judge.cmd_targets(db, TODAY, 5)
        judge.cmd_save_brief(db, {'run_date': TODAY, 'items': [{'pattern_id': pid,
            'event': 'Observed', 'pattern': 'Repeated', 'proposals': ['Investigate']}]}, targets)
        db.conn.execute("UPDATE events SET evidence_json='[\"changed later\"]'")
        from rem import feedback
        verdicts = feedback.parse_journal(report.render(db, TODAY))
        assert [(v.pattern_id, v.decision) for v in verdicts] == [(pid, 'carry')]
        for rendered in (report.render(db, TODAY), html_report.render_html(db, TODAY)):
            assert 'original quote 0' in rendered and 'changed later' not in rendered
            assert 'e0' in rendered and day(-5) in rendered and '1 known sessions' in rendered
    finally:
        db.close()
