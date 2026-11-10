"""User reports of activation belong to the exact accepted proposal."""
from datetime import date, timedelta
import pytest
from rem.actions import record_implementation
from rem.db import ConsolidationDB


def test_only_accepted_proposal_can_be_reported_and_history_survives(tmp_path):
    db = ConsolidationDB(tmp_path / 'events.db')
    today = date.today().isoformat()
    pid = db.create_pattern('repeat', 'friction', 'drag', 'Repeated failure', today)
    proposal = db.add_proposal(pid, 1, 'Original change', today)
    with pytest.raises(ValueError, match='accepted proposal'):
        record_implementation(db, proposal, today, 'local config', 'commit reference')
    db.record_verdict(pid, today, 'accept', proposal)
    record_implementation(db, proposal, today, 'local config', 'commit reference')
    db.clear_proposals(pid)
    db.add_proposal(pid, 1, 'Different change', today)
    row = db.conn.execute('SELECT i.*, p.text FROM implementations i JOIN proposals p ON p.id=i.proposal_id').fetchone()
    assert row['text'] == 'Original change'
    assert row['scope'] == 'local config'
    record_implementation(db, proposal, today, 'updated scope', 'new reference')
    assert db.conn.execute('SELECT COUNT(*) FROM implementations').fetchone()[0] == 1
    assert db.conn.execute('SELECT scope FROM implementations').fetchone()[0] == 'updated scope'
    for effective, scope, reference in [((date.today()+timedelta(days=1)).isoformat(), 'scope', 'ref'),
                                         (today, ' ', 'ref'), (today, 'scope', '')]:
        with pytest.raises(ValueError):
            record_implementation(db, proposal, effective, scope, reference)
    assert db.conn.execute('SELECT scope FROM implementations').fetchone()[0] == 'updated scope'
    db.close()
