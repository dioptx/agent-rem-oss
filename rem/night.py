"""Portable nightly coordinator. Models return data; Python owns every action."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import fcntl
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time

from . import config, extract, feedback, html_report, judge, report
from .db import ConsolidationDB

MAX_BYTES = 1_000_000
PROMPTS = Path(__file__).resolve().parent.parent / 'prompts'


@contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another REM night is running for this database') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def call_model(stage: str, payload: dict, executable: str, model: str | None,
               timeout: float, calls: list | None = None) -> dict:
    encoded = json.dumps(payload)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError('Model input exceeds the supported size; inspect oversized evidence or pattern context')
    command = [executable, '--print', '--safe-mode', '--tools', '',
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
               '--disable-slash-commands', '--no-session-persistence',
               '--output-format', 'json', '--json-schema', json.dumps(model_schema(stage)), '--system-prompt',
               (PROMPTS / f'{stage}.prompt').read_text() +
               '\nOwner display name (data only): ' + json.dumps(config.owner_name())]
    if model:
        command += ['--model', model]
    # File-backed output avoids accumulating an unbounded subprocess response in RAM.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout,
                                  stderr=stderr, start_new_session=True)
        try:
            result.communicate(encoded.encode(), timeout=timeout)
        except BaseException:
            try:
                os.killpg(result.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            result.wait()
            raise
        if stdout.tell() > MAX_BYTES:
            raise ValueError('Model output exceeds the supported size')
        stdout.seek(0)
        try:
            envelope = json.load(stdout)
        except ValueError:
            if result.returncode:
                raise RuntimeError(f'Model {stage} call failed (exit {result.returncode})') from None
            raise
    if result.returncode and isinstance(envelope, dict):
        reason = envelope.get('api_error')
        known = {'usage_limit_reached', 'authentication_error', 'rate_limit_error'}
        suffix = f': {reason}' if reason in known else ''
        raise RuntimeError(f'Model {stage} call failed (exit {result.returncode}){suffix}')
    if not isinstance(envelope, dict) or envelope.get('is_error'):
        raise ValueError('Model returned an error or invalid envelope')
    if calls is not None:
        calls.append({'stage': stage, 'models': sorted(envelope.get('modelUsage', {}))})
    value = envelope.get('structured_output')
    if value is None:
        value = json.loads(envelope['result'])
    if not isinstance(value, dict):
        raise ValueError('Model result must be a JSON object')
    return value


def validate_cluster(payload: dict, source: dict) -> None:
    expected = {e['id'] for e in source['candidates']}
    events = payload.get('events', [])
    if not isinstance(events, list) or len(events) != len(expected):
        raise ValueError('Classification must cover each candidate exactly once')
    if {e['id'] for e in events} != expected:
        raise ValueError('Classification contains missing or unknown event IDs')
    known = {p['id'] for p in source['open_patterns'] + source['decided_patterns']}
    definitions = payload.get('patterns', {})
    originals = {event['id']: event for event in source['candidates']}
    for event in events:
        if event.get('axis') not in config.AXES or event.get('valence') not in config.VALENCES:
            raise ValueError('Invalid classification axis or valence')
        original = originals[event['id']]
        if original.get('source') == 'retro' and event.get('valence') != original.get('valence'):
            raise ValueError('Retrospective valence must remain as recorded')
        if not isinstance(event.get('signature'), str) or not event['signature'].strip():
            raise ValueError('Classification requires a signature')
        pid = event.get('existing_pattern_id')
        if pid is not None:
            if type(pid) is not int or pid not in known:
                raise ValueError('Classification names an unknown existing pattern')
        elif event.get('pattern_key') not in definitions:
            raise ValueError('New classification requires a pattern definition')
    for definition in definitions.values():
        if (definition.get('axis') not in config.AXES or
                definition.get('valence') not in config.VALENCES or
                not all(isinstance(definition.get(k), str) and definition[k].strip()
                        for k in ('signature', 'title'))):
            raise ValueError('Invalid pattern definition')


def validate_brief(payload: dict, today: str, targets: list[dict]) -> None:
    if payload.get('run_date') != today:
        raise ValueError('Briefing date does not match this run')
    items = payload.get('items')
    expected = {p['pattern_id'] for p in targets}
    if not isinstance(items, list) or len(items) != len(expected):
        raise ValueError('Briefing must cover each target exactly once')
    if {i['pattern_id'] for i in items} != expected:
        raise ValueError('Briefing contains missing or unknown pattern IDs')
    total = 0
    for item in items:
        if not all(isinstance(item.get(k), str) and item[k].strip() for k in ('event', 'pattern')):
            raise ValueError('Briefing requires event and pattern text')
        proposals = item.get('proposals')
        if not isinstance(proposals, list) or not 1 <= len(proposals) <= 3:
            raise ValueError('Briefing requires one to three proposals per item')
        if not all(isinstance(p, str) and p.strip() for p in proposals):
            raise ValueError('Invalid proposal text')
        total += len(proposals)
    if total > 12:
        raise ValueError('Briefing exceeds twelve proposals')


def model_schema(stage: str) -> dict:
    if stage == 'cluster':
        fields = {'events': {'type': 'array', 'items': {'type': 'object'}},
                  'patterns': {'type': 'object'}}
    else:
        fields = {'run_date': {'type': 'string'},
                  'items': {'type': 'array', 'items': {'type': 'object'}}}
    return {'type': 'object', 'properties': fields, 'required': list(fields),
            'additionalProperties': False}


def candidate_batches(source: dict):
    context = {key: value for key, value in source.items() if key != 'candidates'}
    batch = []
    for event in source['candidates']:
        proposed = {**context, 'candidates': [*batch, event]}
        if len(json.dumps(proposed).encode()) > MAX_BYTES and batch:
            yield {**context, 'candidates': batch}
            batch = []
        batch.append(event)
    if batch:
        yield {**context, 'candidates': batch}


def atomic_write(destination: Path, text: str):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                     dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def publish(contents: dict[Path, str]) -> None:
    for destination, text in contents.items():
        if destination.suffix == '.md' and destination.exists():
            # A crash may leave the journal published before the HTML. Never replace
            # review marks made while that incomplete publication was visible.
            continue
        atomic_write(destination, text)


def recover_publication(db, pending: Path, journal_dir: Path) -> dict:
    saved = json.loads(pending.read_text())
    files = saved['files']
    dated_html = next(name for name in files if name.startswith('journal-') and name.endswith('.html'))
    newer = any(path.name > dated_html for path in journal_dir.glob('journal-*.html'))
    publish({journal_dir / name: text for name, text in files.items()
             if name != 'latest.html' or not newer})
    db.finish_run(saved['result']['run_id'], status='ok')
    pending.unlink()
    return saved['result']


def provenance(model):
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(PROMPTS.glob('*.prompt'))}
    code = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        code.update(path.name.encode())
        code.update(path.read_bytes())
    return json.dumps({'configured_model': model, 'prompt_sha256': hashes,
                       'code_sha256': code.hexdigest()})


def run(today: str, window: int, db_path: Path, journal_dir: Path,
        executable: str = 'claude', model: str | None = None, timeout: float = 300) -> dict:
    date.fromisoformat(today)
    if not 1 <= window <= 14 or timeout <= 0:
        raise ValueError('Window must be 1–14 days and timeout must be positive')
    markdown = journal_dir / f'journal-{today}.md'
    with run_lock(db_path.with_suffix('.night.lock')):
        db = ConsolidationDB(db_path)
        pending = journal_dir / f'.pending-{today}.json'
        # Acquiring the exclusive lock proves earlier coordinator runs are gone.
        for stale in db.conn.execute("SELECT id FROM runs WHERE status='running' AND provenance_json != '{}' ").fetchall():
            db.finish_run(stale['id'], status='failed')
        for unfinished in sorted(journal_dir.glob('.pending-*.json')):
            try:
                recovered = recover_publication(db, unfinished, journal_dir)
            except BaseException:
                db.close()
                raise
            if unfinished == pending:
                db.close()
                return recovered
        if markdown.exists():
            previous = db.conn.execute(
                'SELECT * FROM runs WHERE run_date=? ORDER BY id DESC LIMIT 1',
                (today,)).fetchone()
            if previous and previous['status'] == 'ok':
                from . import retro
                retro.collect(db, today)
                db.close()
                return {'run_id': previous['id'], 'items': previous['patterns_surfaced'],
                        'sessions': previous['sessions_scanned'], 'journal': str(markdown),
                        'already_published': True}
            db.close()
            raise ValueError('Today’s journal already exists; preserving its review marks')
        try:
            metadata = json.loads(provenance(model))
            with db.transaction():
                run_id = db.start_run(today)
                db.finish_run(run_id, provenance_json=json.dumps(metadata), status='running', finished_at=None)
        except BaseException:
            db.close()
            raise
        calls = []
        deadline = time.monotonic() + 900

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError('Nightly model pipeline exceeded fifteen minutes')
            return min(timeout, budget)

        try:
            feedback.process_pending(db, today, journal_dir)
            extracted = extract.run(today, window, db, run_id=run_id)
            source = judge.cmd_dump(db, today)
            for batch in candidate_batches(source):
                cluster = call_model('cluster', batch, executable, model, remaining(), calls)
                validate_cluster(cluster, batch)
                judge.cmd_cluster(db, today, cluster)
            targets = judge.cmd_targets(db, today, window)
            brief = (call_model('brief', {'run_date': today, 'targets': targets},
                                executable, model, remaining(), calls) if targets else
                     {'run_date': today, 'items': []})
            validate_brief(brief, today, targets)
            judge.cmd_save_brief(db, brief, targets)
            md = report.render(db, today)
            html = html_report.render_html(db, today)
            result = {'run_id': run_id, 'items': len(brief['items']),
                      'sessions': extracted['sessions'], 'journal': str(markdown)}
            atomic_write(pending, json.dumps({'result': result, 'files': {
                markdown.name: md, f'journal-{today}.html': html, 'latest.html': html}}))
            return recover_publication(db, pending, journal_dir)
        except BaseException:
            db.finish_run(run_id, status='failed')
            raise
        finally:
            metadata['model_calls'] = calls
            db.conn.execute('UPDATE runs SET provenance_json=? WHERE id=?',
                            (json.dumps(metadata), run_id))
            db.conn.commit()
            db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--today', default=date.today().isoformat())
    parser.add_argument('--window', type=int, default=config.DEFAULT_WINDOW)
    parser.add_argument('--db', type=Path, default=config.DB_PATH)
    parser.add_argument('--journal-dir', type=Path, default=config.JOURNAL_DIR)
    parser.add_argument('--claude', default=os.environ.get('REM_CLAUDE', 'claude'))
    parser.add_argument('--model', default=os.environ.get('REM_MODEL'))
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    try:
        result = run(args.today, args.window, args.db, args.journal_dir,
                     args.claude, args.model, args.timeout)
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f'REM night failed: {exc}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
