"""Settings: defaults, validation, env > file > default, atomic save that logs only real changes."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from rem import settings


def test_defaults_match_the_documented_values():
    v = settings.values(path=settings.Path("/nonexistent/settings.json"), env={})
    assert v["window_days"] == 14
    assert v["surface_limit"] == 5
    assert v["surface_min_occurrences"] == 3
    assert v["escalation_cap"] == 7
    assert v["escalation_factor"] == 0.5
    assert v["weight_error"] == 1.5 and v["weight_loop"] == 0.5


def test_file_overrides_default_and_env_overrides_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"window_days": 7, "surface_limit": 4}))
    v = settings.values(path=p, env={})
    assert v["window_days"] == 7 and v["surface_limit"] == 4
    eff = settings.effective(path=p, env={"CONSOLIDATE_WINDOW": "9"})
    assert eff["window_days"]["value"] == 9
    assert eff["window_days"]["locked_by"] == "CONSOLIDATE_WINDOW"
    assert eff["surface_limit"]["source"] == "file"


@pytest.mark.parametrize("changes, message", [
    ({"window_days": 0}, "window_days must be between 1 and 14"),
    ({"surface_limit": "five"}, "surface_limit must be a number"),
    ({"no_such_key": 1}, "unknown setting: no_such_key"),
    ({"surface_limit": 2.5}, "surface_limit must be a whole number"),
    ({"surface_limit": True}, "surface_limit must be a number"),
    ({"surface_limit": "inf"}, "surface_limit must be a number"),
    ({"surface_limit": "nan"}, "surface_limit must be a number"),
    (["not", "a", "dict"], "settings must be a JSON object"),
])
def test_invalid_changes_are_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        settings.validate(changes)


def test_save_writes_atomically_and_reports_only_changed_keys(tmp_path):
    p = tmp_path / "settings.json"
    changed = settings.save({"surface_limit": 4, "window_days": 14}, path=p, env={})
    assert changed == [("surface_limit", 5, 4)]          # window_days equals its default
    assert json.loads(p.read_text()) == {"surface_limit": 4}
    assert settings.save({"surface_limit": 4}, path=p, env={}) == []   # replay is a no-op
    assert not list(tmp_path.glob("*.tmp"))


def test_save_refuses_env_locked_keys(tmp_path):
    with pytest.raises(ValueError, match="set by CONSOLIDATE_WINDOW"):
        settings.save({"window_days": 7}, path=tmp_path / "s.json", env={"CONSOLIDATE_WINDOW": "5"})


def test_reset_to_default_removes_the_key(tmp_path):
    p = tmp_path / "settings.json"
    settings.save({"surface_limit": 4}, path=p, env={})
    assert settings.save({"surface_limit": 5}, path=p, env={}) == [("surface_limit", 4, 5)]
    assert json.loads(p.read_text()) == {}


def test_bad_env_value_names_its_variable(tmp_path):
    with pytest.raises(ValueError, match="CONSOLIDATE_WINDOW='30'"):
        settings.effective(path=tmp_path / "missing.json", env={"CONSOLIDATE_WINDOW": "30"})


def test_corrupt_file_fails_loudly(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="settings file is not valid JSON"):
        settings.values(path=p, env={})


def _config_value(expr: str, settings_path, extra_env=None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CONSOLIDATE_WINDOW"}
    env["REM_SETTINGS"] = str(settings_path)
    env.update(extra_env or {})
    out = subprocess.run([sys.executable, "-c", f"from rem import config; print({expr})"],
                         cwd=settings.REPO_DIR, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_config_reads_saved_settings(tmp_path):
    p = tmp_path / "settings.json"
    settings.save({"surface_limit": 3, "weight_error": 2.0, "window_days": 7}, path=p, env={})
    assert _config_value("config.SURFACE_LIMIT", p) == "3"
    assert _config_value("config.KIND_SEVERITY['error']", p) == "2.0"
    assert _config_value("config.DEFAULT_WINDOW", p) == "7"
    assert _config_value("config.DEFAULT_WINDOW", p, {"CONSOLIDATE_WINDOW": "4"}) == "4"


def test_nightly_prompt_has_no_unfilled_placeholders():
    text = (settings.REPO_DIR / "prompts" / "consolidation.prompt").read_text()
    assert "{{" not in text
    assert "python3 -m rem.extract --today TODAY" in text   # window comes from settings
    assert "mcp__agentsview__search_content" in text        # 30-day corroboration past the window


_NOFILE = settings.Path("/nonexistent/settings.json")


def test_mode_defaults_equal_spec_defaults():
    for key, (_label, _help, options, default) in settings.MODES.items():
        for setting, value in options[default].items():
            assert value == settings.SPEC[setting][0], (key, setting)


def test_modes_report_default_current_and_env_lock():
    rows = settings.modes(path=_NOFILE, env={})
    assert [r["key"] for r in rows] == ["lookback", "briefing", "pickiness", "nagging", "focus"]
    assert {r["key"]: r["current"] for r in rows} == {
        "lookback": "2 weeks", "briefing": "Normal", "pickiness": "Balanced",
        "nagging": "Normal", "focus": "Balanced"}
    assert all(r["locked_by"] is None for r in rows)
    assert rows[0]["options"] == ["3 days", "5 days", "1 week", "2 weeks"]
    rows = settings.modes(path=_NOFILE, env={"CONSOLIDATE_WINDOW": "7"})
    assert rows[0]["current"] == "1 week" and rows[0]["locked_by"] == "CONSOLIDATE_WINDOW"
    assert rows[1]["locked_by"] is None


def test_save_modes_writes_values_and_custom_is_none(tmp_path):
    path = tmp_path / "settings.json"
    changes = settings.save_modes({"briefing": "Short", "nagging": "Persistent"}, path=path, env={})
    assert {c[0] for c in changes} == {"surface_limit", "escalation_factor", "escalation_cap"}
    stored = json.loads(path.read_text())
    assert (stored["surface_limit"], stored["escalation_factor"], stored["escalation_cap"]) == (3, 1.0, 14)
    cur = {r["key"]: r["current"] for r in settings.modes(path=path, env={})}
    assert cur["briefing"] == "Short" and cur["nagging"] == "Persistent"
    settings.save({"surface_limit": 4}, path=path, env={})
    assert {r["key"]: r["current"] for r in settings.modes(path=path, env={})}["briefing"] is None


def test_save_modes_rejects_unknown_and_locked(tmp_path):
    path = tmp_path / "settings.json"
    with pytest.raises(ValueError, match="nope"):
        settings.save_modes({"nope": "Short"}, path=path, env={})
    with pytest.raises(ValueError, match="Huge"):
        settings.save_modes({"briefing": "Huge"}, path=path, env={})
    with pytest.raises(ValueError, match="CONSOLIDATE_WINDOW"):
        settings.save_modes({"lookback": "1 week"}, path=path, env={"CONSOLIDATE_WINDOW": "3"})


def test_unknown_key_in_file_is_ignored_with_a_warning(tmp_path, capsys):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"surface_limit": 4, "old_key": 1}))
    assert settings.values(path=f, env={})["surface_limit"] == 4
    assert "old_key" in capsys.readouterr().err
