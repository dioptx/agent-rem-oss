"""Installer safety checks."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_install_refuses_existing_non_symlink(tmp_path):
    destination = tmp_path / ".local" / "bin" / "rem"
    destination.parent.mkdir(parents=True)
    destination.write_text("keep me", encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path)}

    result = subprocess.run(
        [str(ROOT / "scripts" / "install.sh")],
        env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode != 0
    assert destination.read_text(encoding="utf-8") == "keep me"
    assert "Refusing to replace existing non-symlink" in result.stderr


def test_install_links_all_surfaces_in_clean_home(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(ROOT / "scripts" / "install.sh")],
        env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    for relative in (
        ".local/bin/rem",
        ".claude/commands/agent-rem.md",
        ".claude/weft/templates/rem-review-act.json",
        ".codex/weft-templates/rem-review-act.json",
        ".codex/skills/agent-rem",
    ):
        assert (tmp_path / relative).is_symlink(), relative
    assert not (tmp_path / ".agents").exists()
