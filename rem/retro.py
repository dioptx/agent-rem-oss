"""Pull Weft's fixed retro.md format into REM. File contents are data only."""
from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from .extract import redact


def roots() -> list[Path]:
    # Claude's projects directory is a transcript store, not a source checkout.
    # This repo has no common source-project root, so discovery is opt-in.
    return [Path(value).expanduser() for value in os.environ.get('REM_RETRO_ROOTS', '').split(os.pathsep) if value]


def parse(path: Path, text: str, today: str) -> tuple[dict, list[dict]]:
    if '\x00' in text:
        raise ValueError('retro contains a NUL character')
    lines = text.splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError('missing frontmatter')
    end = lines.index('---', 1)
    metadata = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(':')
        if not separator or key.strip() in metadata:
            raise ValueError('invalid frontmatter')
        metadata[key.strip()] = value.strip().strip('"\'')
    if set(metadata) != {'plan', 'project', 'started', 'finished'}:
        raise ValueError('frontmatter needs plan, project, started and finished')
    if metadata['plan'] != path.parent.name or not re.fullmatch(r'[\w.-]+', metadata['plan']):
        raise ValueError('plan slug must match its folder')
    if not re.fullmatch(r'[\w.-]+(?:/[\w.-]+)?', metadata['project']):
        raise ValueError('invalid project name')
    start = date.fromisoformat(metadata['started'])
    finish = date.fromisoformat(metadata['finished'] or today)
    if start > finish:
        raise ValueError('finished precedes started')

    phase = section = None
    blocks = {}
    current = None
    fence = None
    for line in lines[end + 1:]:
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            fence = None if fence == marker[1][0] else marker[1][0]
            continue
        if fence:
            continue
        if line.startswith('## '):
            phase, section, current = line[3:].strip(), None, None
            if phase not in ('Planning', 'Build-out'):
                raise ValueError('unknown phase')
            blocks.setdefault(phase, {})
        elif line.startswith('### '):
            section, current = line[4:].strip(), None
            if phase is None or section not in ('Went well', 'Fell short', 'Change next time'):
                raise ValueError('unknown retro section')
            blocks[phase].setdefault(section, [])
        elif line.startswith('- '):
            if phase is None or section is None:
                raise ValueError('item outside a retro section')
            current = [line[2:].strip()]
            blocks[phase][section].append(current)
        elif line.strip() and current is not None:
            current.append(line.strip())
        elif line.strip():
            raise ValueError('unexpected text outside an item')
    if 'Planning' not in blocks:
        raise ValueError('missing Planning phase')
    events, repeated = [], Counter()
    metadata['changes'] = []
    for phase, sections in blocks.items():
        changes = [redact('Change next time: ' + ' '.join(parts)) for parts in sections.get('Change next time', [])
                   if ' '.join(parts).casefold() != 'none.']
        metadata['changes'].extend({'phase': phase, 'evidence': change} for change in changes)
        for section in ('Went well', 'Fell short'):
            for parts in sections.get(section, []):
                raw = ' '.join(parts)
                evidence = re.search(r'\bEvidence:\s*(\S.*)', raw, re.I)
                if not evidence:
                    continue
                if section == 'Went well':
                    kind, valence = 'learning', 'lift'
                elif raw.startswith('[drag]'):
                    kind, valence = 'correction', 'drag'
                elif raw.startswith('[defect]'):
                    kind, valence = 'defect', 'drag'
                    if not raw.startswith(f"[defect] {metadata['project']}/{metadata['plan']}:"):
                        raise ValueError('defect must name this project/plan')
                else:
                    continue
                digest = hashlib.sha256(raw.encode()).hexdigest()
                repeated[(phase, kind, digest)] += 1
                identity = json.dumps([str(path), phase, kind, digest, repeated[(phase, kind, digest)]])
                item = redact(raw)
                events.append(dict(
                    id='retro-' + hashlib.sha256(identity.encode()).hexdigest(),
                    run_date=today, occurred_date=finish.isoformat(), source='retro', agent='weft',
                    project=metadata['project'], plan_slug=metadata['plan'], phase=phase,
                    plan_started=start.isoformat(), plan_finished=finish.isoformat(), retro_path=str(path),
                    kind=kind, valence=valence, title=item, detail=item,
                    evidence=[item, *changes], content_hash=digest))
    return metadata, events


def _dispatch(db, errors):
    command = os.environ.get('REM_DEFECT_COMMAND', '')
    if not command:
        return
    try:
        argv = shlex.split(command)
        if not argv:
            raise ValueError()
    except ValueError:
        errors.append('REM_DEFECT_COMMAND is not a valid argument list')
        return
    defects = db.conn.execute("SELECT * FROM events WHERE source='retro' AND kind='defect'").fetchall()
    for event in defects:
        # The event identity includes the item's content hash. Keep receipts when
        # a file changes, so appending Build-out cannot file the same defect twice.
        with db.transaction():
            claimed = db.conn.execute(
                "INSERT OR IGNORE INTO retro_defect_calls(event_id, status) VALUES (?, 'attempted')",
                (event['id'],)).rowcount
        if not claimed:
            continue
        status = 'ok'
        try:
            result = subprocess.run(
                [*argv, f"{event['project']}/{event['plan_slug']}", event['detail']],
                shell=False, capture_output=True, text=True, timeout=30)
            if result.returncode:
                status = 'failed'
        except (OSError, subprocess.TimeoutExpired):
            status = 'failed'
        with db.transaction():
            db.conn.execute('UPDATE retro_defect_calls SET status=? WHERE event_id=?', (status, event['id']))
        if status == 'failed':
            errors.append(f"defect command failed for {event['project']}/{event['plan_slug']}")


def collect(db, today: str, dry_run: bool = False) -> dict:
    date.fromisoformat(today)
    events, errors, seen, refreshed = [], [], set(), False
    for root in roots():
        try:
            paths = sorted(root.glob('*/planning/*/retro.md'))
        except OSError:
            errors.append(f'{root}: cannot search retro root')
            continue
        for candidate in paths:
            path = candidate
            try:
                path = candidate.resolve()
                if path in seen:
                    continue
                seen.add(path)
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                previous = db.conn.execute('SELECT content_hash FROM retro_files WHERE path=?', (str(path),)).fetchone()
                if previous and previous['content_hash'] == digest:
                    continue
                metadata, items = parse(path, raw.decode('utf-8'), today)
                if not dry_run:
                    with db.transaction():
                        old = {row['id']: dict(row) for row in db.conn.execute(
                            'SELECT * FROM events WHERE source=\'retro\' AND retro_path=?', (str(path),))}
                        db.conn.execute("DELETE FROM events WHERE source='retro' AND retro_path=?", (str(path),))
                        for item in items:
                            if item['id'] in old:
                                item.update({key: old[item['id']][key] for key in ('signature', 'axis', 'pattern_id')})
                            db.insert_event(item)
                        db.conn.execute(
                            'INSERT INTO retro_files(path, content_hash, ingested_on, metadata_json) VALUES (?,?,?,?) '
                            'ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash, '
                            'ingested_on=excluded.ingested_on, metadata_json=excluded.metadata_json',
                            (str(path), digest, today, json.dumps(metadata)))
                    refreshed = True
                events.extend(items)
            except (OSError, UnicodeError, ValueError, RuntimeError) as error:
                errors.append(f'{path}: malformed or unreadable retro ({type(error).__name__})')
    if not dry_run:
        _dispatch(db, errors)
        if refreshed:
            db.recompute_stats(today)
    for error in errors:
        print('retro: ' + error, file=sys.stderr)
    return {'events': len(events), 'candidates': events, 'errors': errors}


def defect_lines(db) -> list[str]:
    rows = db.conn.execute(
        "SELECT e.*, c.status AS dispatch_status FROM events e LEFT JOIN retro_defect_calls c ON c.event_id=e.id "
        "WHERE e.source='retro' AND e.kind='defect' ORDER BY e.occurred_date, e.id").fetchall()
    if not rows:
        return []
    return ['## Defects to file', '', *[
        f"- {row['project']}/{row['plan_slug']} · {row['phase']} · "
        f"{row['detail'].replace('<', '&lt;').replace('>', '&gt;')} "
        f"(defect command: {row['dispatch_status'] or 'not configured/not sent'})" for row in rows], '']
