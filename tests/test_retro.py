"""Plan retrospectives use synthetic files and databases, never personal stores."""
import importlib
import json
import shlex
import subprocess
import sys

import pytest

from rem import config, extract, judge, ledger, night, report, triage
from rem.db import ConsolidationDB

TODAY = '2026-11-10'


def write_retro(root, slug='example-plan', finished=TODAY, body=None):
    path = root / 'example-project' / 'planning' / slug / 'retro.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nplan: {slug}\nproject: example-project\nstarted: 2026-11-02\nfinished: {finished}\n---\n\n'
                    + (body if body is not None else
                       '## Planning\n### Went well\n- Tests caught a regression. Evidence: agenda.test.py passed.\n'
                       '### Fell short\n- [drag] The same target had to be explained. Evidence: two corrections.\n'
                       '### Change next time\n- Check the target before changing files.\n'))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('REM_RETRO_ROOTS', str(tmp_path / 'projects'))
    monkeypatch.delenv('REM_DEFECT_COMMAND', raising=False)
    monkeypatch.setattr(extract, 'iter_session_files', lambda *args: [])
    monkeypatch.setattr(config, 'JOURNAL_DIR', tmp_path / 'journals')
    db = ConsolidationDB(tmp_path / 'events.db')
    yield db, tmp_path / 'projects'
    db.close()


def collect(db, **kwargs):
    return importlib.import_module('rem.retro').collect(db, TODAY, **kwargs)


def test_extract_automatically_ingests_redacted_retro_with_change_evidence(store):
    db, root = store
    path = write_retro(root)
    path.write_text(path.read_text().replace('two corrections', 'token=abcdefghijklmnopqrstuv two corrections'))
    extract.run(TODAY, 14, db)
    rows = db.conn.execute('SELECT * FROM events ORDER BY valence').fetchall()
    assert len(rows) == 2
    assert {r['source'] for r in rows} == {'retro'}
    assert {r['plan_slug'] for r in rows} == {'example-plan'}
    assert {r['phase'] for r in rows} == {'Planning'}
    assert {r['valence'] for r in rows} == {'drag', 'lift'}
    assert all('Change next time' in r['evidence_json'] for r in rows)
    assert 'abcdefghijklmnopqrstuv' not in str([dict(r) for r in rows])
    assert '[REDACTED]' in str([dict(r) for r in rows])


def test_unchanged_file_adds_nothing(store):
    db, root = store
    write_retro(root)
    collect(db)
    before = [dict(r) for r in db.conn.execute('SELECT * FROM events ORDER BY id')]
    result = collect(db)
    assert result['events'] == 0
    assert [dict(r) for r in db.conn.execute('SELECT * FROM events ORDER BY id')] == before


def test_changed_file_replaces_earlier_items_and_keeps_planning_once(store):
    db, root = store
    path = write_retro(root, finished='')
    collect(db)
    path.write_text(path.read_text().replace('finished: \n', f'finished: {TODAY}\n')
                    + '\n## Build-out\n### Went well\n- Delivery worked. Evidence: release test passed.\n'
                      '### Fell short\n- None.\n### Change next time\n- Keep the release check.\n')
    collect(db)
    rows = db.conn.execute('SELECT * FROM events').fetchall()
    assert len(rows) == 3
    assert sum(r['phase'] == 'Planning' for r in rows) == 2
    path.write_text(path.read_text().replace('Delivery worked.', 'Delivery was verified.'))
    collect(db)
    assert db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 3
    assert not db.conn.execute("SELECT 1 FROM events WHERE detail LIKE '%Delivery worked.%'").fetchone()


def test_recurrence_counts_one_per_plan_not_one_per_source(store):
    db, root = store
    pid = db.create_pattern('target', 'friction', 'drag', 'Target drift', TODAY)
    for eid, source, project, day, phase in (
        ('retro-a', 'retro', 'example-project', '2026-11-10', 'Planning'),
        ('retro-b', 'retro', 'example-project', '2026-11-10', 'Build-out'),
        ('transcript-a', 'transcript', 'example-project', '2026-11-03', None),
        ('transcript-b', 'transcript', 'example-project', '2026-11-08', None),
        ('other-project', 'transcript', 'another-project', '2026-11-08', None),
        ('before-plan', 'transcript', 'example-project', '2026-11-01', None),
    ):
        db.insert_event(dict(id=eid, run_date=TODAY, occurred_date=day, project=project,
                             source=source, plan_slug='example-plan' if source == 'retro' else None,
                             phase=phase, plan_started='2026-11-02', plan_finished=TODAY,
                             kind='correction', signature='target', pattern_id=pid))
    db.recompute_stats(TODAY, 14)
    assert db.get_pattern(pid)['occurrence_count'] == 3


def test_unfinished_plan_uses_its_ingest_date(store):
    db, root = store
    write_retro(root, finished='', body='## Planning\n### Fell short\n- [drag] Target drift. Evidence: two corrections.\n')
    collect(db)
    row = db.conn.execute('SELECT * FROM events').fetchone()
    assert row['plan_finished'] == TODAY
    pid = db.create_pattern('target', 'friction', 'drag', 'Target', TODAY)
    db.tag_event(row['id'], 'friction', 'drag', 'target')
    db.assign_event(row['id'], pid)
    for day in (TODAY, '2026-11-11'):
        db.insert_event(dict(id=day, run_date=day, occurred_date=day, project='example-project',
                             kind='correction', signature='target', pattern_id=pid))
    db.recompute_stats('2026-11-11', 14)
    assert db.get_pattern(pid)['occurrence_count'] == 2


def test_defects_are_reported_and_never_enter_patterns(store):
    db, root = store
    write_retro(root, body='## Planning\n### Fell short\n- [defect] example-project/example-plan: Button failed. Evidence: button.test.py.\n')
    collect(db)
    row = db.conn.execute('SELECT * FROM events').fetchone()
    assert row['kind'] == 'defect'
    assert db.candidates() == [] and judge.cmd_dump(db, TODAY)['candidates'] == []
    judge.cmd_cluster(db, TODAY, {'events': [dict(id=row['id'], axis='friction', valence='drag',
                                               signature='button', pattern_key='button')], 'patterns': {}})
    assert db.conn.execute('SELECT COUNT(*) FROM patterns').fetchone()[0] == 0
    for rendered in (ledger.render(db), report.render(db, TODAY)):
        assert 'Defects to file' in rendered
        assert 'Button failed' in rendered


def test_defect_command_once_per_item_content_even_when_buildout_is_added(store, monkeypatch):
    db, root = store
    path = write_retro(root, body='## Planning\n### Fell short\n- [defect] example-project/example-plan: Button failed. Evidence: button.test.py.\n')
    retro = importlib.import_module('rem.retro')
    calls = []
    def record(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setenv('REM_DEFECT_COMMAND', 'fake-defect --file')
    monkeypatch.setattr(retro.subprocess, 'run', record)
    collect(db)
    collect(db)
    path.write_text(path.read_text() + '\n## Build-out\n### Went well\n- Shipped. Evidence: release check.\n')
    collect(db)
    assert len(calls) == 1
    assert calls[0][0][:2] == ['fake-defect', '--file']
    assert calls[0][0][-2] == 'example-project/example-plan'
    assert calls[0][1].get('shell', False) is False
    path.write_text(path.read_text().replace('Button failed.', 'Button failed again.'))
    collect(db)
    assert len(calls) == 2


def test_hostile_item_is_one_argument_and_cannot_run_shell_text(store, monkeypatch, tmp_path):
    db, root = store
    marker, output = tmp_path / 'must-not-exist', tmp_path / 'args.json'
    hostile = f'$(touch {marker}); echo bad && `touch {marker}`'
    write_retro(root, body=f'## Planning\n### Fell short\n- [defect] example-project/example-plan: {hostile}. Evidence: failure test.\n')
    recorder = tmp_path / 'recorder.py'
    recorder.write_text('import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n')
    monkeypatch.setenv('REM_DEFECT_COMMAND', shlex.join([sys.executable, str(recorder), str(output)]))
    collect(db)
    args = json.loads(output.read_text())
    assert len(args) == 2 and args[0] == 'example-project/example-plan'
    assert hostile in args[1]
    assert not marker.exists()


def test_malformed_file_is_reported_and_skipped_without_losing_valid_items(store, capsys):
    db, root = store
    write_retro(root)
    bad = write_retro(root, slug='bad-plan')
    bad.write_text('---\nplan: bad-plan\nstarted: not-a-date\n---\n')
    result = collect(db)
    assert len(result['errors']) == 1
    assert db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 2
    assert 'bad-plan' in capsys.readouterr().err


def test_embedded_nul_is_reported_before_a_defect_command_can_abort_collection(store, monkeypatch):
    db, root = store
    write_retro(root)
    write_retro(root, slug='bad-plan', body='## Planning\n### Fell short\n'
                '- [defect] example-project/bad-plan: Broken\x00item. Evidence: failure test.\n')
    monkeypatch.setenv('REM_DEFECT_COMMAND', shlex.join([sys.executable, '-c', 'pass']))
    result = collect(db)
    assert len(result['errors']) == 1
    assert db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='defect'").fetchone()[0] == 0


def test_unresolvable_retro_path_does_not_stop_neighboring_files(store, monkeypatch):
    from pathlib import Path
    db, root = store
    write_retro(root)
    write_retro(root, slug='bad-plan')
    resolve = Path.resolve
    def resolve_candidate(path, *args, **kwargs):
        if path.parent.name == 'bad-plan':
            raise OSError('synthetic path resolution failure')
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve_candidate)
    result = collect(db)
    assert len(result['errors']) == 1
    assert db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 2


def test_items_without_evidence_and_none_are_not_ingested(store):
    db, root = store
    write_retro(root, body='## Planning\n### Went well\n- No proof here.\n- None.\n### Fell short\n- [drag] No proof either.\n')
    collect(db)
    assert db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0


def test_change_next_time_is_retained_even_without_an_observation(store):
    db, root = store
    write_retro(root, body='## Planning\n### Went well\n- None.\n### Fell short\n- None.\n'
                           '### Change next time\n- Check the target before changing files.\n')
    collect(db)
    assert db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
    assert 'Check the target before changing files.' in ledger.render(db)


def test_roots_are_opt_in_and_colon_separated(monkeypatch, tmp_path):
    retro = importlib.import_module('rem.retro')
    monkeypatch.delenv('REM_RETRO_ROOTS', raising=False)
    assert retro.roots() == []
    monkeypatch.setenv('REM_RETRO_ROOTS', f'{tmp_path}/one:{tmp_path}/two')
    assert retro.roots() == [tmp_path / 'one', tmp_path / 'two']


def test_triage_collect_ingests_without_a_remote_transport(store, monkeypatch):
    db, root = store
    write_retro(root)
    monkeypatch.setattr(config, 'DB_PATH', db.db_path)
    monkeypatch.setattr(triage, 'FETCH_CMD', '')
    assert triage.collect() == 0
    assert db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 2


def test_scheduled_run_ingests_without_a_manual_step(store, monkeypatch, tmp_path):
    db, root = store
    write_retro(root)
    def model(stage, payload, *args):
        if stage == 'cluster':
            return {'events': [dict(id=e['id'], axis='friction', valence=e['valence'],
                                    signature=e['id'], pattern_key=e['id']) for e in payload['candidates']],
                    'patterns': {e['id']: dict(signature=e['id'], axis='friction', valence=e['valence'],
                                              title='Synthetic observation') for e in payload['candidates']}}
        return {'run_date': TODAY, 'items': [dict(pattern_id=p['pattern_id'], event='Observed result',
                                                pattern='One plan', proposals=['Keep the check']) for p in payload['targets']]}
    monkeypatch.setattr(night, 'call_model', model)
    result = night.run(TODAY, 14, db.db_path, tmp_path / 'journals')
    assert result['items'] == 2
    assert db.conn.execute('SELECT COUNT(*) FROM events WHERE source="retro"').fetchone()[0] == 2


def test_owner_and_remote_defaults_are_neutral_and_explicit_settings_survive(monkeypatch):
    from rem import telegram
    for key in ('REM_OWNER_NAME', 'REM_REMOTE_HOST', 'REM_ASK_CMD', 'REM_TRIAGE_FETCH'):
        monkeypatch.delenv(key, raising=False)
    assert config.owner_name() == 'the user'
    monkeypatch.setenv('REM_OWNER_NAME', 'Example Owner')
    assert config.owner_name() == 'Example Owner'
    try:
        importlib.reload(telegram)
        importlib.reload(triage)
        assert not telegram.ASK_CMD and not triage.FETCH_CMD
        monkeypatch.setenv('REM_REMOTE_HOST', 'example.invalid')
        importlib.reload(telegram)
        importlib.reload(triage)
        assert 'example.invalid' in telegram.ASK_CMD and 'example.invalid' in triage.FETCH_CMD
        monkeypatch.setenv('REM_ASK_CMD', 'custom-ask --local')
        monkeypatch.setenv('REM_TRIAGE_FETCH', 'custom-fetch --local')
        importlib.reload(telegram)
        importlib.reload(triage)
        assert telegram.ASK_CMD == 'custom-ask --local'
        assert triage.FETCH_CMD == 'custom-fetch --local'
    finally:
        # Leave no configured transport for other tests.
        monkeypatch.delenv('REM_REMOTE_HOST', raising=False)
        monkeypatch.delenv('REM_ASK_CMD', raising=False)
        monkeypatch.delenv('REM_TRIAGE_FETCH', raising=False)
        importlib.reload(telegram)
        importlib.reload(triage)
