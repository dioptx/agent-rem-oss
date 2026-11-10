"""Shared paths and constants. Standard-lib only, host-agnostic.

Paths resolve from $HOME so the package runs unchanged on any machine. Override any of them via the matching CONSOLIDATE_* env var.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import settings as _settings

HOME = Path.home()


def owner_name() -> str:
    """Optional display name; no personal identity is built into prompts."""
    return os.environ.get('REM_OWNER_NAME') or 'the user'


def _path(env: str, default: Path) -> Path:
    val = os.environ.get(env)
    return Path(val).expanduser() if val else default


# Agent transcript stores. Keep CONSOLIDATE_PROJECTS_DIR as the backwards-
# compatible Claude override; Codex has its own nested, date-partitioned store.
CLAUDE_PROJECTS_DIR = _path(
    "CONSOLIDATE_CLAUDE_PROJECTS_DIR",
    _path("CONSOLIDATE_PROJECTS_DIR", HOME / ".claude" / "projects"),
)
CODEX_SESSIONS_DIR = _path(
    "CONSOLIDATE_CODEX_SESSIONS_DIR", HOME / ".codex" / "sessions"
)
CODEX_ARCHIVE_DIR = _path(
    "CONSOLIDATE_CODEX_ARCHIVE_DIR", HOME / ".codex" / "archived_sessions"
)

# Append-only events store, queryable by future agents. Derived from this
# package's own location so renaming or relocating the checkout cannot silently
# strand the store behind a fresh empty one.
REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = _path("CONSOLIDATE_DB", REPO_DIR / "data" / "events.db")

# Morning markdown briefings (one file per run, never overwritten).
JOURNAL_DIR = _path("CONSOLIDATE_JOURNAL_DIR", HOME / ".local" / "share" / "consolidation")

# Tunable knobs come from data/settings.json (written by the rem serve control panel),
# with env vars winning and the documented defaults as the fallback. Read once per process.
_S = _settings.values()

# Trailing window in days; recency-weighted toward the previous day.
DEFAULT_WINDOW = _S["window_days"]

# How many patterns the briefing surfaces, and the recurrence bar to clear.
SURFACE_LIMIT = _S["surface_limit"]
SURFACE_MIN_OCCURRENCES = _S["surface_min_occurrences"]

# Escalation: a recurring pattern left unaddressed across briefings climbs.
# rank = weighted_score * (1 + min(streak, CAP) * FACTOR). A week of neglect on
# a live friction ~4.5x's its score, floating it to the top with a Day-N badge.
ESCALATION_CAP = _S["escalation_cap"]
ESCALATION_FACTOR = _S["escalation_factor"]

# The four axes (user's vocabulary) plus valence (the one synthesis).
AXES = ("autonomy", "convergence", "friction", "introspection")
VALENCES = ("drag", "lift")

# Severity multiplier per event kind, applied on top of recency in the score.
# Concrete, fixable signals (a tool actually failed, the human refused an
# action) weigh more than the correction/loop keyword heuristics, which
# over-fire on long sessions. Defaults: error 1.5, approval 1.4, denial 1.3,
# learning 1.0, correction 0.7, loop 0.5 (see rem/settings.py).
KIND_SEVERITY = {k: _S[f"weight_{k}"] for k in _settings.KINDS}


def kind_severity(kind: str | None) -> float:
    """Confidence/severity weight for an event kind (default 1.0 for unknowns)."""
    return KIND_SEVERITY.get(kind or "", 1.0)


def recency_weight(days_ago: int, window: int = DEFAULT_WINDOW) -> int:
    """Weight an event by how recently it happened.

    The previous day dominates.
    Today and yesterday weigh heaviest, then a step-down across the window.
    Anything outside the window contributes nothing.
    """
    if days_ago < 0 or days_ago > window:
        return 0
    if days_ago <= 1:
        return 3
    if days_ago == 2:
        return 2
    return 1
