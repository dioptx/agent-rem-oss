"""Runner contracts without a live model, scheduler, or user's transcript store."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rem import extract, night
from rem.db import ConsolidationDB


def fake_model(tmp_path, body):
    path = tmp_path / 'fake claude'
    path.write_text(f'#!{sys.executable}\n' + body)
    path.chmod(0o755)
    return str(path)


def test_model_no_tools_contract(tmp_path):
    exe = fake_model(tmp_path, '''import json, sys
args = sys.argv[1:]
assert args[args.index('--tools') + 1] == ''
assert '--safe-mode' in args and '--strict-mcp-config' in args
assert '--no-session-persistence' in args
assert json.load(sys.stdin) == {'hello': 'world'}
print(json.dumps({'result': json.dumps({'events': []})}))
''')
    assert night.call_model('cluster', {'hello': 'world'}, exe, None, 5) == {'events': []}


def test_model_timeout(tmp_path):
    exe = fake_model(tmp_path, 'import time\ntime.sleep(10)\n')
    with pytest.raises(subprocess.TimeoutExpired):
        night.call_model('cluster', {}, exe, None, .05)


def test_empty_night_and_preserve_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, 'iter_session_files', lambda *a: [])
    path, journals = tmp_path / 'events.db', tmp_path / 'journal'
    result = night.run('2026-01-02', 3, path, journals, executable='must-not-run')
    assert result['items'] == 0
    journal = Path(result['journal'])
    journal.write_text('my review marks')
    assert night.run('2026-01-02', 3, path, journals)['already_published']
    assert journal.read_text() == 'my review marks'
    db = ConsolidationDB(path)
    assert db.conn.execute('SELECT status FROM runs').fetchall()[0][0] == 'ok'
    db.close()


def test_model_failure_preserves_last_good(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, 'iter_session_files', lambda *a: [])
    path, journals = tmp_path / 'events.db', tmp_path / 'journal'
    journals.mkdir()
    (journals / 'latest.html').write_text('last good')
    db = ConsolidationDB(path)
    db.insert_event({'id': 'candidate', 'run_date': '2026-01-02',
                     'occurred_date': '2026-01-02', 'kind': 'error', 'title': 'failure'})
    db.close()
    exe = fake_model(tmp_path, "print('not JSON')\n")
    with pytest.raises(ValueError):
        night.run('2026-01-02', 3, path, journals, executable=exe)
    assert (journals / 'latest.html').read_text() == 'last good'
    assert not (journals / 'journal-2026-01-02.md').exists()
    db = ConsolidationDB(path)
    assert db.conn.execute('SELECT status FROM runs').fetchone()[0] == 'failed'
    db.close()


def test_unknown_cluster_id_rejected():
    with pytest.raises(ValueError):
        night.validate_cluster({'events': [{'id': 'wrong'}]},
                               {'candidates': [{'id': 'right'}]})


def test_lock_excludes_second_runner(tmp_path):
    with night.run_lock(tmp_path / 'lock'):
        with pytest.raises(RuntimeError, match='Another REM'):
            with night.run_lock(tmp_path / 'lock'):
                pass


def test_installed_empty_first_run(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'HOME': str(tmp_path),
           'CONSOLIDATE_DB': str(tmp_path / 'events.db'),
           'CONSOLIDATE_JOURNAL_DIR': str(tmp_path / 'journals'),
           'CONSOLIDATE_CLAUDE_PROJECTS_DIR': str(tmp_path / 'claude'),
           'CONSOLIDATE_CODEX_SESSIONS_DIR': str(tmp_path / 'codex'),
           'CONSOLIDATE_CODEX_ARCHIVE_DIR': str(tmp_path / 'archive')}
    subprocess.run([str(root / 'scripts/install.sh')], env=env, check=True, capture_output=True)
    result = subprocess.run([str(tmp_path / '.local/bin/rem'), 'night', '--claude',
                             'must-not-run'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['sessions'] == 0
    assert (tmp_path / 'journals/latest.html').exists()


def test_publication_failure_recovers_without_losing_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, 'iter_session_files', lambda *a: [])
    path, journals = tmp_path / 'events.db', tmp_path / 'journal'
    original = night.atomic_write
    failed = False

    def fail_html(destination, text):
        nonlocal failed
        if destination.suffix == '.html' and not failed:
            failed = True
            raise OSError('simulated disk failure')
        original(destination, text)

    monkeypatch.setattr(night, 'atomic_write', fail_html)
    with pytest.raises(OSError):
        night.run('2026-01-02', 3, path, journals, executable='must-not-run')
    (journals / 'journal-2026-01-02.md').write_text('human review while interrupted')
    result = night.run('2026-01-02', 3, path, journals, executable='must-not-run')
    assert result['run_id'] == 1
    assert (journals / 'journal-2026-01-02.md').read_text() == 'human review while interrupted'
    assert (journals / 'latest.html').exists()
    assert not (journals / '.pending-2026-01-02.json').exists()


def test_nonempty_fake_model_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, 'iter_session_files', lambda *a: [])
    path, journals = tmp_path / 'events.db', tmp_path / 'journal'
    db = ConsolidationDB(path)
    for i in range(3):
        db.insert_event({'id': str(i), 'run_date': '2026-01-02',
                         'occurred_date': '2026-01-02', 'session_id': str(i),
                         'kind': 'error', 'title': 'Repeated test failure'})
    db.close()
    exe = fake_model(tmp_path, '''import json, sys
p = json.load(sys.stdin)
if 'candidates' in p:
    out = {'events': [dict(id=e['id'], axis='friction', valence='drag', signature='test failure', pattern_key='test') for e in p['candidates']], 'patterns': {'test': dict(axis='friction', valence='drag', signature='test failure', title='Test failure')}}
else:
    out = {'run_date': p['run_date'], 'items': [dict(pattern_id=t['pattern_id'], event='Tests failed', pattern='Tests failed repeatedly', proposals=['Fix the failing test']) for t in p['targets']]}
print(json.dumps({'structured_output': out}))
''')
    assert night.run('2026-01-02', 3, path, journals, executable=exe)['items'] == 1
    db = ConsolidationDB(path)
    assert not db.candidates()
    assert json.loads(db.conn.execute('SELECT provenance_json FROM runs').fetchone()[0])['code_sha256']
    db.close()


def test_next_day_recovers_previous_publication_and_stale_run(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, 'iter_session_files', lambda *a: [])
    path, journals = tmp_path / 'events.db', tmp_path / 'journal'
    db = ConsolidationDB(path)
    stale = db.start_run('2026-01-01')
    db.finish_run(stale, provenance_json='{"configured_model":null}', status='running', finished_at=None)
    db.close()
    journals.mkdir()
    pending = {'result': {'run_id': stale, 'items': 0, 'sessions': 0,
                          'journal': str(journals / 'journal-2026-01-01.md')},
               'files': {'journal-2026-01-01.md': 'old md',
                         'journal-2026-01-01.html': 'old html', 'latest.html': 'old html'}}
    (journals / '.pending-2026-01-01.json').write_text(json.dumps(pending))
    night.run('2026-01-02', 3, path, journals, executable='must-not-run')
    assert (journals / 'journal-2026-01-01.html').read_text() == 'old html'
    assert not list(journals.glob('.pending-*.json'))
    assert (journals / 'latest.html').read_text() != 'old html'
    db = ConsolidationDB(path)
    assert not db.conn.execute("SELECT 1 FROM runs WHERE status='running'").fetchone()
    db.close()


def test_provenance_failure_leaves_no_orphan_run(tmp_path, monkeypatch):
    def fail(_):
        raise OSError('cannot read prompt')
    monkeypatch.setattr(night, 'provenance', fail)
    path = tmp_path / 'events.db'
    with pytest.raises(OSError):
        night.run('2026-01-02', 3, path, tmp_path / 'journal')
    db = ConsolidationDB(path)
    assert db.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    db.close()
