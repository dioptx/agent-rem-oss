"""REM's tunable settings: defaults, validation, and a small JSON file the control panel writes.

Precedence per key: environment variable > data/settings.json > default. Nothing here imports
rem.config, because config reads its constants from this module.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = Path(os.environ.get("REM_SETTINGS", REPO_DIR / "data" / "settings.json")).expanduser()

KINDS = ("error", "approval", "denial", "learning", "correction", "loop")
_KIND_DEFAULTS = {"error": 1.5, "approval": 1.4, "denial": 1.3,
                  "learning": 1.0, "correction": 0.7, "loop": 0.5}
_KIND_HELP = {
    "error": "a tool call failed",
    "approval": "work stopped to ask for approval",
    "denial": "you refused or blocked an action",
    "learning": "the agent picked up a lesson",
    "correction": "you corrected the agent (keyword match, noisy)",
    "loop": "a session went back and forth 3+ times",
}

# key -> (default, type, low, high, env var, label, help)
SPEC: dict[str, tuple] = {
    "window_days": (14, int, 1, 14, "CONSOLIDATE_WINDOW", "Look-back window (days)",
                    "How many past days each nightly run reads."),
    "surface_limit": (5, int, 1, 10, None, "Patterns per briefing",
                      "The most patterns one morning briefing shows."),
    "surface_min_occurrences": (3, int, 1, 10, None, "Repeats before a pattern shows",
                                "A pattern needs this many repeats in the window to be shown first."),
    "escalation_factor": (0.5, float, 0.0, 2.0, None, "Escalation per unresolved day",
                          "How much faster an ignored pattern climbs for each day it carries."),
    "escalation_cap": (7, int, 1, 30, None, "Escalation cap (days)",
                       "After this many days an ignored pattern stops climbing."),
}
for _k in KINDS:
    SPEC[f"weight_{_k}"] = (_KIND_DEFAULTS[_k], float, 0.0, 3.0, None,
                           f"Weight: {_k}", f"How much it counts when {_KIND_HELP[_k]}.")

_FOCUS_BALANCED = {f"weight_{k}": _KIND_DEFAULTS[k] for k in KINDS}

# group -> (label, help, {option: {setting key: value}}, default option)
MODES: dict[str, tuple] = {
    "lookback": ("How far back I read", "How many past days I read each night.",
                 {"3 days": {"window_days": 3}, "5 days": {"window_days": 5},
                  "1 week": {"window_days": 7}, "2 weeks": {"window_days": 14}}, "2 weeks"),
    "briefing": ("How long my morning note is", "How many things one morning note shows.",
                 {"Short": {"surface_limit": 3}, "Normal": {"surface_limit": 5},
                  "Long": {"surface_limit": 8}}, "Normal"),
    "pickiness": ("How many repeats before I speak up",
                  "How often a thing must repeat before I mention it.",
                  {"Catch early": {"surface_min_occurrences": 2},
                   "Balanced": {"surface_min_occurrences": 3},
                   "Only strong": {"surface_min_occurrences": 5}}, "Balanced"),
    "nagging": ("How loud I get when you ignore something",
                "How fast an ignored thing climbs back to the top.",
                {"Gentle": {"escalation_factor": 0.25, "escalation_cap": 5},
                 "Normal": {"escalation_factor": 0.5, "escalation_cap": 7},
                 "Persistent": {"escalation_factor": 1.0, "escalation_cap": 14}}, "Normal"),
    "focus": ("What I care about most", "Which kinds of moments I weigh the most.",
              {"Balanced": _FOCUS_BALANCED,
               "Tool errors first": {"weight_error": 2.0, "weight_approval": 1.6, "weight_denial": 1.5,
                                     "weight_learning": 1.0, "weight_correction": 0.5,
                                     "weight_loop": 0.3},
               "My corrections first": {"weight_error": 1.2, "weight_approval": 1.2,
                                        "weight_denial": 1.3, "weight_learning": 1.0,
                                        "weight_correction": 1.2, "weight_loop": 0.8}}, "Balanced"),
}


def _coerce(key: str, raw) -> int | float:
    default, typ, low, high, *_ = SPEC[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"{key} must be a number")
    try:
        num = float(raw)
    except (ValueError, OverflowError):
        raise ValueError(f"{key} must be a number") from None
    if not math.isfinite(num):
        raise ValueError(f"{key} must be a number")
    if typ is int:
        if num != int(num):
            raise ValueError(f"{key} must be a whole number")
        num = int(num)
    if not low <= num <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return num


def validate(changes: dict) -> dict:
    """Coerce and range-check every key; raise ValueError naming each problem."""
    if not isinstance(changes, dict):
        raise ValueError("settings must be a JSON object")
    problems, clean = [], {}
    for key, raw in changes.items():
        if key not in SPEC:
            problems.append(f"unknown setting: {key}")
            continue
        try:
            clean[key] = _coerce(key, raw)
        except ValueError as e:
            problems.append(str(e))
    if problems:
        raise ValueError("; ".join(problems))
    return clean


def read_file(path: Path = SETTINGS_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError(f"settings file is not valid JSON: {path}") from None
    if isinstance(data, dict):
        for key in [k for k in data if k not in SPEC]:
            print(f"rem: ignoring unknown setting {key} in {path}", file=sys.stderr)
            del data[key]
    return validate(data)


def effective(path: Path = SETTINGS_PATH, env=None) -> dict[str, dict]:
    """Every setting with its value, default, source (env/file/default), lock and help text."""
    env = os.environ if env is None else env
    stored = read_file(path)
    out = {}
    for key, (default, typ, low, high, env_var, label, help_) in SPEC.items():
        row = {"value": default, "default": default, "source": "default", "locked_by": None,
               "label": label, "help": help_, "min": low, "max": high,
               "step": 1 if typ is int else 0.1}
        if key in stored:
            row.update(value=stored[key], source="file")
        if env_var and env.get(env_var):
            try:
                value = _coerce(key, env[env_var])
            except ValueError as e:
                raise ValueError(f"{env_var}={env[env_var]!r}: {e}") from None
            row.update(value=value, source="env", locked_by=env_var)
        out[key] = row
    return out


def values(path: Path = SETTINGS_PATH, env=None) -> dict:
    return {k: row["value"] for k, row in effective(path, env).items()}


_SAVE_LOCK = threading.Lock()


def save(changes: dict, path: Path = SETTINGS_PATH, env=None, db=None,
         source: str = "ui") -> list[tuple]:
    """Validate, write atomically, return (key, old, new) for keys whose value changed.

    A value equal to its default is removed from the file, so the file only holds overrides.
    With `db` (a ConsolidationDB), each change is also appended to settings_log.
    """
    with _SAVE_LOCK:
        return _save(changes, path, env, db, source)


def _save(changes, path, env, db, source) -> list[tuple]:
    env = os.environ if env is None else env
    path = Path(path)
    clean = validate(changes)
    current = effective(path, env)
    locked = [f"{k} is set by {current[k]['locked_by']}" for k in clean if current[k]["locked_by"]]
    if locked:
        raise ValueError("; ".join(locked))
    stored = read_file(path)
    changed = []
    for key, new in clean.items():
        old = current[key]["value"]
        if new == old:
            continue
        changed.append((key, old, new))
        if new == SPEC[key][0]:
            stored.pop(key, None)
        else:
            stored[key] = new
    if not changed:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(stored, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if db is not None:
        for key, old, new in changed:
            db.log_setting(key, old, new, source)
    return changed


def modes(path: Path = SETTINGS_PATH, env=None) -> list[dict]:
    """Each mode group with its option names, the option matching the effective values (None = custom)."""
    eff = effective(path, env)
    out = []
    for key, (label, help_, options, _default) in MODES.items():
        current = next((name for name, vals in options.items()
                        if all(eff[k]["value"] == v for k, v in vals.items())), None)
        keys = sorted({k for vals in options.values() for k in vals})
        locked_by = next((eff[k]["locked_by"] for k in keys if eff[k]["locked_by"]), None)
        out.append({"key": key, "label": label, "help": help_, "options": list(options),
                    "current": current, "locked_by": locked_by})
    return out


def save_modes(choices: dict, path: Path = SETTINGS_PATH, env=None, db=None,
               source: str = "ui") -> list[tuple]:
    """Translate {group: option} into the numeric settings and save them."""
    if not isinstance(choices, dict):
        raise ValueError("modes must be a JSON object")
    merged = {}
    for group, option in choices.items():
        if group not in MODES:
            raise ValueError(f"unknown mode group: {group}")
        options = MODES[group][2]
        if not isinstance(option, str) or option not in options:
            raise ValueError(f"unknown option for {group}: {option}")
        merged.update(options[option])
    return save(merged, path=path, env=env, db=db, source=source)
