"""Execute follow-up render functions against the API fixture without a server."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rem import stats
from tests.test_stats import conn, fixes, TODAY  # noqa: F401


def test_followup_render_and_copy_text_are_descriptive(fixes, tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the page render functions')
    page = Path(__file__).parents[1] / 'rem/static/index.html'
    source = page.read_text().split('<script>')[1].split('</script>')[0]
    source = source[:source.rindex('load().catch')]
    overview = stats.overview(fixes, TODAY, tmp_path / 'absent')
    overview['accepted'][0]['implementations'] = [{
        'proposal_id': 1, 'proposal': 'Use scoped checks', 'effective_on': '2026-09-20',
        'scope': '<script>unsafe</script>', 'reference': 'commit recorded by user'}]
    script = r'''
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = {};
const context = {document: {addEventListener() {}, getElementById(id) {
  return elements[id] ||= {innerHTML:'', textContent:''};
}}, window: {innerWidth:1000, addEventListener() {}}, URL, console};
vm.createContext(context);
vm.runInContext(input.source, context);
context.payload = input.overview;
vm.runInContext('renderTiles(payload); renderFixes(payload.fixes); renderYes(payload);', context);
const copy = vm.runInContext('retryAsk(payload.fixes[0])', context);
process.stdout.write(JSON.stringify({elements, copy}));
'''
    result = subprocess.run([node, '-e', script], input=json.dumps({'source': source, 'overview': overview}),
                            text=True, capture_output=True, check=True)
    rendered = json.loads(result.stdout)
    text = json.dumps(rendered)
    for forbidden in ('made things quieter', "didn't stick", 'stronger fix', 'ready to grade'):
        assert forbidden not in text
    assert 'linked event groups' in rendered['elements']['fixes']['innerHTML']
    assert 'days in scan windows' in rendered['elements']['fixes']['innerHTML']
    assert 'Check the evidence and whether' in rendered['copy']
    assert 'not impact' in rendered['elements']['tiles']['innerHTML']

    assert 'User-reported implementation' in rendered['elements']['yes']['innerHTML']
    assert 'Not independently verified' in rendered['elements']['yes']['innerHTML']
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in rendered['elements']['yes']['innerHTML']


def test_overview_metrics_keep_agent_denominators_dates_and_gaps(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the page render functions')
    source = (Path(__file__).parents[1] / 'rem/static/index.html').read_text().split('<script>')[1].split('</script>')[0]
    source = source[:source.rindex('load().catch')]
    overview = {
        'today': '2026-10-28',
        'tiles': {'friction': {'week': '2026-10-19'}, 'waiting': {'count': 4},
                  'fixes': {'with_evidence': 2, 'accepted': 5, 'coverage_unavailable': 1}},
        'friction': [
            {'agent': 'claude', 'week': '2026-10-19', 'sessions': 8, 'with_friction': 3, 'rate': 3/8, 'gap': None},
            {'agent': 'codex', 'week': '2026-10-19', 'sessions': 0, 'with_friction': 0, 'rate': None, 'gap': 'transcripts pruned'}]}
    script = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = {};
const context = {document: {addEventListener() {}, getElementById(id) {
  return elements[id] ||= {innerHTML:'', textContent:''};
}}, window: {innerWidth:390, addEventListener() {}}, URL, console};
vm.createContext(context); vm.runInContext(input.source, context);
context.payload = input.overview;
vm.runInContext('renderTiles(payload)', context);
const full = elements.tiles.innerHTML;
context.payload.tiles.friction.week = null;
context.payload.friction = [];
vm.runInContext('renderTiles(payload)', context);
process.stdout.write(JSON.stringify({full, empty:elements.tiles.innerHTML}));
"""
    result = subprocess.run([node, '-e', script], input=json.dumps({'source': source, 'overview': overview}),
                            text=True, capture_output=True, check=True)
    rendered = json.loads(result.stdout)
    assert '3 of 8' in rendered['full']
    assert '2026-10-19 to 2026-10-25' in rendered['full']
    assert 'non-automated sessions started that week' in rendered['full']
    assert 'Not available' in rendered['full'] and 'those sessions are gone' in rendered['full']
    assert '0 of 0' not in rendered['full']
    assert 'As of 2026-10-28' in rendered['full']
    assert 'No measurable full week' in rendered['empty']
    assert 'NaN' not in rendered['empty']
