"""Accepted fixes get linked to their Rem-Fixes commits without a hand-run step."""

from __future__ import annotations

import json
import subprocess

from rem import config, link
from rem.db import ConsolidationDB

TODAY = "2026-10-27"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


def repo_with(tmp_path, messages, remote="git@github.com:acme/widgets.git"):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", remote)
    for i, msg in enumerate(messages):
        (repo / f"f{i}").write_text(str(i))
        git(repo, "add", f"f{i}")
        git(repo, "commit", "-q", "-m", msg)
    return repo


def accepted(db, title, status="accepted"):
    pid = db.create_pattern(title, "friction", "drag", title, TODAY)
    prop = db.add_proposal(pid, 1, "do the thing")
    db.conn.execute("INSERT INTO verdicts (proposal_id, pattern_id, run_date, decision, created_at) "
                    "VALUES (?,?,?,?,?)", (prop, pid, TODAY, "accept", TODAY))
    db.set_pattern_status(pid, status)
    db.conn.commit()
    return pid


def test_a_marked_commit_links_the_accepted_thing(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    fixed = accepted(db, "Patch misses")
    reopened = accepted(db, "Guessed paths", status="open")   # came back after the yes
    untouched = accepted(db, "Slow extract")
    hushed = accepted(db, "Gate friction", status="muted")
    repo = repo_with(tmp_path, ["unrelated work",
                                f"Re-read before patching\n\nRem-Fixes: {fixed}, {reopened}",
                                f"Also\n\nRem-Fixes: {hushed}"])
    out = link.link_fixes(db, TODAY, roots={repo})
    got = {x["pattern_id"]: x["url"] for x in out}
    assert set(got) == {fixed, reopened}
    assert got[fixed].startswith("https://github.com/acme/widgets/commit/")
    row = lambda pid: db.conn.execute("SELECT fix_pr FROM patterns WHERE id=?", (pid,)).fetchone()[0]
    assert row(fixed) == got[fixed] and row(untouched) is None and row(hushed) is None
    db.close()


def test_an_already_linked_thing_keeps_its_link(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    pid = accepted(db, "Patch misses")
    db.set_fix_pr(pid, "https://github.com/acme/widgets/pull/7")
    repo = repo_with(tmp_path, [f"Later\n\nRem-Fixes: {pid}"])
    assert link.link_fixes(db, TODAY, roots={repo}) == []
    assert db.conn.execute("SELECT fix_pr FROM patterns WHERE id=?", (pid,)).fetchone()[0].endswith("/pull/7")
    db.close()


def test_dry_run_writes_nothing_and_a_non_github_remote_still_links(tmp_path):
    db = ConsolidationDB(tmp_path / "events.db")
    pid = accepted(db, "Patch misses")
    repo = repo_with(tmp_path, [f"Fix\n\nRem-Fixes: {pid}"], remote="https://gitlab.example/acme/w.git")
    out = link.link_fixes(db, TODAY, roots={repo}, dry_run=True)
    assert out and out[0]["url"].startswith(str(repo) + "@")
    assert db.conn.execute("SELECT fix_pr FROM patterns WHERE id=?", (pid,)).fetchone()[0] is None
    db.close()


def test_session_repos_come_from_the_transcripts_cwd(tmp_path, monkeypatch):
    repo = repo_with(tmp_path, ["init"])
    projects = tmp_path / "projects" / "p"
    projects.mkdir(parents=True)
    (projects / "s1.jsonl").write_text(json.dumps({"type": "user", "cwd": str(repo / ".")}) + "\n")
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "none")
    monkeypatch.setattr(config, "CODEX_ARCHIVE_DIR", tmp_path / "none")
    assert link.session_repos(14, TODAY) == {repo.resolve()} or link.session_repos(14, TODAY) == {repo}
