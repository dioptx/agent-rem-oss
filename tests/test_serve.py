"""rem serve endpoints against a real server on an ephemeral port."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from rem import serve
from rem.db import ConsolidationDB
from tests.test_control import FakeLaunchctl

RUN = "2026-09-30"
JOURNAL = """# Night consolidation — {run}

## 1 ▼ DRAG · friction · Patch misses · ×3 (score 3) `[Day 1 · unresolved]` <!--P:{pid}-->
**Proposals**
- [ ] T1 — re-read before patching <!--p:{prop}-->
  note:

---
"""


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "events.db"
    ConsolidationDB(db).close()
    journals = tmp_path / "journals"
    journals.mkdir()
    ctx = serve.Ctx(db_path=db, journal_dir=journals, settings_path=tmp_path / "settings.json",
                    heartbeat=tmp_path / "history.jsonl", runner=FakeLaunchctl(),
                    today=lambda: RUN)
    srv = serve.make_server(0, ctx)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, json.dumps(body).encode() if body is not None else None,
                 {"Content-Type": "application/json", **(headers or {})})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def api(srv, method, path, body=None, headers=None):
    status, raw = call(srv, method, path, body, headers)
    return status, json.loads(raw)


def allowed(srv):
    return {"X-REM": "1", "Origin": f"http://127.0.0.1:{srv.server_address[1]}"}


def test_page_and_overview_load(server):
    status, raw = call(server, "GET", "/")
    assert status == 200 and b"<title>" in raw
    status, data = api(server, "GET", "/api/overview")
    assert status == 200 and data["ok"] and "headline" in data
    assert data["shift"]["state"] == "none" and data["accepted"] == []


def test_writes_need_the_header_and_our_origin(server):
    body = {"changes": {"surface_limit": 4}}
    assert api(server, "POST", "/api/settings", body)[0] == 403
    evil = {"X-REM": "1", "Origin": "http://evil.example"}
    assert api(server, "POST", "/api/settings", body, evil)[0] == 403


def test_settings_save_validates_and_logs(server):
    status, data = api(server, "POST", "/api/settings", {"changes": {"surface_limit": 4}},
                       allowed(server))
    assert status == 200 and data["changed"] == [{"key": "surface_limit", "old": 5, "new": 4}]
    status, data = api(server, "POST", "/api/settings", {"changes": {"surface_limit": 99}},
                       allowed(server))
    assert status == 400 and "between 1 and 10" in data["error"]
    db = ConsolidationDB(server.ctx.db_path)
    assert tuple(db.conn.execute("SELECT key, new FROM settings_log").fetchone()) == ("surface_limit", "4")
    db.close()


def test_settings_modes_listed_and_saved_by_name(server):
    status, data = api(server, "GET", "/api/settings")
    assert status == 200 and "surface_limit" in data["settings"]
    assert {m["key"] for m in data["modes"]} >= {"lookback", "pickiness"}
    status, data = api(server, "POST", "/api/settings", {"modes": {"pickiness": "Only strong"}},
                       allowed(server))
    assert status == 200
    assert data["changed"] == [{"key": "surface_min_occurrences", "old": 3, "new": 5}]
    status, data = api(server, "POST", "/api/settings", {"modes": {"pickiness": "Extreme"}},
                       allowed(server))
    assert status == 400 and "Extreme" in data["error"]


def test_control_action_changes_the_job_and_is_logged(server):
    status, data = api(server, "POST", "/api/control", {"action": "pause"}, allowed(server))
    assert status == 200 and data["job"]["paused"] is True
    assert api(server, "POST", "/api/control", {"action": "rm -rf"}, allowed(server))[0] == 400
    db = ConsolidationDB(server.ctx.db_path)
    assert tuple(db.conn.execute("SELECT key, new FROM settings_log").fetchone()) == ("nightly job", "pause")
    db.close()


def test_triage_save_marks_the_journal_and_logs_verdicts(server):
    db = ConsolidationDB(server.ctx.db_path)
    pid = db.create_pattern("patch misses", "friction", "drag", "Patch misses", RUN)
    prop = db.add_proposal(pid, 1, "re-read before patching")
    db.add_brief(RUN, pid, 1, "ev", "pat", 1, "smallest fix")
    db.close()
    journal = server.ctx.journal_dir / f"journal-{RUN}.md"
    journal.write_text(JOURNAL.format(run=RUN, pid=pid, prop=prop), encoding="utf-8")

    status, data = api(server, "GET", "/api/triage")
    assert status == 200 and data["run_date"] == RUN and data["items"][0]["logged"] is None

    item = {"pattern_id": pid, "accept": [1], "action": "accept", "note": ""}
    status, data = api(server, "POST", "/api/rem-triage", {"run_date": RUN, "items": [item]},
                       allowed(server))
    assert status == 200 and data["logged"] == [{"title": "Patch misses",
                                                 "verdict": "accept (1 proposal(s))"}]
    assert "- [x] T1" in journal.read_text()
    status, data = api(server, "GET", "/api/triage")
    assert data["items"][0]["logged"] == "accept (1 proposal(s))"

    status, data = api(server, "POST", "/api/rem-triage", {"run_date": "2026-09-01", "items": [item]},
                       allowed(server))
    assert status == 400 and "newest briefing" in data["error"]


def test_foreign_host_is_refused(server):
    port = server.server_address[1]
    assert api(server, "GET", "/api/overview", headers={"Host": f"evil.example:{port}"})[0] == 403
    status, data = api(server, "POST", "/api/control", {"action": "pause"},
                       {**allowed(server), "Host": "evil.example"})
    assert status == 403 and data["error"] == "forbidden host"
    assert api(server, "GET", "/api/overview", headers={"Host": f"localhost:{port}"})[0] == 200


def test_responses_refuse_framing(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert resp.getheader("Content-Security-Policy") == "frame-ancestors 'none'"


def test_malformed_triage_bodies_are_rejected_before_any_write(server):
    journal = server.ctx.journal_dir / f"journal-{RUN}.md"
    journal.write_text(JOURNAL.format(run=RUN, pid=1, prop=1), encoding="utf-8")
    before = journal.read_bytes()
    bad = [5, [1], [{"pattern_id": "1"}], [{"pattern_id": True}], [{"pattern_id": 1, "accept": "12"}],
           [{"pattern_id": 1, "accept": [True]}], [{"pattern_id": 1, "action": 3}]]
    for items in bad:
        status, _ = api(server, "POST", "/api/rem-triage", {"run_date": RUN, "items": items},
                        allowed(server))
        assert status == 400, items
    assert journal.read_bytes() == before


def test_negative_content_length_is_a_400(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.putrequest("POST", "/api/settings")
    for k, v in {**allowed(server), "Content-Length": "-1"}.items():
        conn.putheader(k, v)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400 and b"bad Content-Length" in resp.read()
    conn.close()


def test_tailnet_requests_need_the_configured_host_and_login(server):
    tail = {"Host": "mbp.example.ts.net:3043", "X-Forwarded-For": "100.64.0.9",
            "Tailscale-User-Login": "me@example.com"}
    assert api(server, "GET", "/api/overview", headers=tail)[0] == 403    # not configured
    server.ctx.tailnet_host, server.ctx.tailnet_login = "mbp.example.ts.net:3043", "me@example.com"
    assert api(server, "GET", "/api/overview", headers=tail)[0] == 200
    assert api(server, "GET", "/api/overview",
               headers={**tail, "Tailscale-User-Login": "coworker@example.com"})[0] == 403
    no_login = {k: v for k, v in tail.items() if k != "Tailscale-User-Login"}
    assert api(server, "GET", "/api/overview", headers=no_login)[0] == 403   # tagged node
    assert api(server, "GET", "/api/overview",
               headers={**tail, "Host": "evil.example:3043"})[0] == 403
    port = server.server_address[1]
    assert api(server, "GET", "/api/overview",
               headers={**tail, "Host": f"127.0.0.1:{port}",
                        "X-Forwarded-Host": "mbp.example.ts.net:3043"})[0] == 200
    status, _ = api(server, "POST", "/api/control", {"action": "pause"}, {**tail, **allowed(server)})
    assert status == 403                                                # local origin via tailnet
    status, data = api(server, "POST", "/api/settings", {"modes": {"briefing": "Short"}},
                       {**tail, "X-REM": "1", "Origin": "https://mbp.example.ts.net:3043"})
    assert status == 200 and data["ok"]


def test_presenter_mode_refuses_every_write_and_strips_private_text(server):
    db = ConsolidationDB(server.ctx.db_path)
    pid = db.create_pattern("patch misses", "friction", "drag", "Patch misses", RUN)
    prop = db.add_proposal(pid, 1, "re-read ~/Projects/secret-repo/x.py before patching")
    db.add_brief(RUN, pid, 1, "ev in /Users/someone/private", "pat", 1, "because ~/.codex")
    db.close()
    journal = server.ctx.journal_dir / f"journal-{RUN}.md"
    journal.write_text(JOURNAL.format(run=RUN, pid=pid, prop=prop), encoding="utf-8")
    before = journal.read_text()
    server.ctx.present = True

    item = {"pattern_id": pid, "accept": [1], "action": "accept", "note": ""}
    for path, body in (("/api/rem-triage", {"run_date": RUN, "items": [item]}),
                       ("/api/settings", {"changes": {"surface_limit": 4}}),
                       ("/api/control", {"action": "run_now"})):
        status, data = api(server, "POST", path, body, allowed(server))
        assert status == 403 and "presenting" in data["error"]
    assert journal.read_text() == before
    assert not [c for c in server.ctx.runner.calls if c[0] not in ("print", "print-disabled")]

    status, raw = call(server, "GET", "/api/triage")
    assert status == 200 and b"secret-repo" not in raw and b"/Users/someone" not in raw and b"~/.codex" not in raw
    data = json.loads(raw)
    assert data["present"] is True and data["items"][0]["title"] == "Patch misses"
    status, data = api(server, "GET", "/api/overview")
    assert data["present"] is True and data["shift"]["log"] is None


def test_scrub_overview_keeps_numbers_and_drops_text():
    o = {"headline": "h", "shift": {"state": "ok", "log": "/home/me/logs/y.txt"},
         "accepted": [{"title": "t", "ideas": [{"text": "edit ~/.config/agent/rules"}], "note": "n",
                       "fix_pr": "https://github.com/private/repo/pull/1"}],
         "fixes": [{"title": "t", "fix_pr": "https://github.com/private/repo/pull/2", "before": 4, "after": 1}],
         "runs": [{"ts": "t", "status": "ok", "summary": "private gate friction"}],
         "tiles": {"runs": {"ok": 1, "last": {"status": "ok", "summary": "private recap"}}}}
    s = serve.scrub_overview(o)
    assert "private" not in json.dumps(s) and "~/.config" not in json.dumps(s)
    assert s["accepted"][0]["made"] is True and s["fixes"][0]["before"] == 4 and s["shift"]["state"] == "ok"
