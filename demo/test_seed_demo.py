"""Exercise the synthetic ledger through REM's real APIs, CLI and page endpoints."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from seed_demo import NIGHTS, PATTERNS, seed_demo
from serve_demo import demo_context


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    folder = tmp_path / "demo"
    monkeypatch.setenv("REM_SETTINGS", str(folder / "settings.json"))
    manifest = seed_demo(folder, "2026-10-31")
    return folder, manifest


def test_two_seeds_match_every_row_and_generated_file(tmp_path, monkeypatch):
    monkeypatch.setenv("REM_SETTINGS", str(tmp_path / "unused-settings.json"))
    left, right = tmp_path / "left", tmp_path / "right"
    assert seed_demo(left, "2026-10-31") == seed_demo(right, "2026-10-31")
    left_files = {p.relative_to(left): p.read_bytes() for p in left.rglob("*") if p.is_file()}
    right_files = {p.relative_to(right): p.read_bytes() for p in right.rglob("*") if p.is_file()}
    assert left_files == right_files  # includes SQLite bytes, all clocks and journal text


def test_overview_has_nights_friction_decisions_quiet_recurrence_and_untreated(seeded):
    from rem import stats, triage
    from rem.db import ConsolidationDB
    folder, manifest = seeded
    conn = stats.connect(folder / "events.db")
    try:
        overview = stats.overview(conn, manifest["today"], folder / "history.jsonl")
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == NIGHTS
        assert conn.execute("SELECT count(*) FROM patterns").fetchone()[0] == len(PATTERNS)
        assert overview["tiles"]["waiting"]["count"] == 5
        assert len({r["week"] for r in overview["friction"] if r["rate"] is not None}) >= 8
        assert {r["agent"] for r in overview["friction"]} == {"claude", "codex"}
        assert len({r["rate"] for r in overview["friction"] if r["rate"] is not None}) > 2
        fixes = {r["pattern_id"]: r for r in overview["fixes"]}
        for pid in manifest["quiet_patterns"]:
            assert fixes[pid]["before"] > 0 and fixes[pid]["after"] == 0
            assert fixes[pid]["observed_after"] > 0
        returned = fixes[manifest["returned_pattern"]]
        assert returned["after"] > returned["before"] > 0
        accepted = {r["pattern_id"]: r for r in overview["accepted"]}
        assert accepted[manifest["returned_pattern"]]["back"] is True
        assert len(accepted) == 3
        for pid in manifest["untreated_patterns"]:
            assert conn.execute("SELECT count(*) FROM verdicts WHERE pattern_id=?", (pid,)).fetchone()[0] == 0
        assert any(row["quiet"] > 0 for row in overview["backlog"])
        assert sum(r["status"] == "failed" for r in overview["runs"]) == 1
        assert overview["shift"]["state"] == "failing"
    finally:
        conn.close()
    db = ConsolidationDB(folder / "events.db")
    try:
        cards = triage.cards(db, manifest["latest_journal"])
        assert len(cards) == 5
        assert all(card["proposals"] and card["recommend_tier"] == 1 for card in cards)
    finally:
        db.close()
    journal = folder / "journals" / f"journal-{manifest['latest_journal']}.md"
    assert "<!--P:" in journal.read_text() and "- [ ]" in journal.read_text()


def test_refuses_to_open_an_existing_directory(tmp_path):
    (tmp_path / "keep.txt").write_text("untouched")
    with pytest.raises(ValueError, match="empty demo directory"):
        seed_demo(tmp_path)
    assert (tmp_path / "keep.txt").read_text() == "untouched"
    assert not (tmp_path / "events.db").exists()


def test_real_cli_reads_the_seed(seeded):
    folder, manifest = seeded
    env = dict(os.environ, CONSOLIDATE_DB=str(folder / "events.db"),
               CONSOLIDATE_JOURNAL_DIR=str(folder / "journals"), REM_SETTINGS=str(folder / "settings.json"))
    result = subprocess.run([sys.executable, "-m", "rem.ledger", "--runs", "3"], env=env,
                            text=True, capture_output=True, check=True)
    assert "56 runs" in result.stdout and "Decisions logged: 3" in result.stdout
    result = subprocess.run([sys.executable, "-m", "rem.actions", "--preview", "--today", manifest["latest_journal"]],
                            env=env, text=True, capture_output=True, check=True)
    assert "Asked to repeat requirements" in result.stdout


def test_real_page_and_api_use_only_the_demo_context(seeded):
    from rem.serve import Handler
    folder, manifest = seeded
    server = SimpleNamespace(ctx=demo_context(folder), server_address=("127.0.0.1", 3043))

    class Request:
        # Exercise the real HTTP parser and endpoint code without opening a socket.
        def __init__(self, endpoint):
            self.input = io.BytesIO(f"GET {endpoint} HTTP/1.0\r\nHost: 127.0.0.1:3043\r\n\r\n".encode())
            self.output = io.BytesIO()

        def makefile(self, *_args):
            return self.input

        def sendall(self, data):
            self.output.write(data)

        def settimeout(self, _timeout):
            pass

    for endpoint in ("/", "/api/overview", "/api/triage", "/api/settings", "/api/control"):
        request = Request(endpoint)
        Handler(request, ("127.0.0.1", 12345), server)
        headers, _, data = request.output.getvalue().partition(b"\r\n\r\n")
        assert b" 200 " in headers.splitlines()[0], (endpoint, data)
        if endpoint == "/":
            assert b'id="triage"' in data and b'id="fixes"' in data
        elif endpoint == "/api/triage":
            assert len(json.loads(data)["items"]) == 5
        elif endpoint == "/api/overview":
            assert json.loads(data)["today"] == manifest["today"]
        elif endpoint == "/api/control":
            assert json.loads(data)["job"]["loaded"] is False
