"""Pause, resume and run the nightly consolidation job through launchctl.

Fixed argv, never a shell. `runner` is injectable so tests never touch launchd.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

LABEL = os.environ.get("CONSOLIDATE_JOB_LABEL", "org.agent-rem.night")
PLIST = Path(os.environ.get(
    "CONSOLIDATE_JOB_PLIST",
    str(Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"),
)).expanduser()


class ControlError(RuntimeError):
    pass


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(argv: list[str], runner):
    try:
        return runner(argv, capture_output=True, text=True, timeout=20)
    except FileNotFoundError as exc:
        raise ControlError(
            "launchctl is unavailable; run rem night manually or configure your scheduler"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ControlError("scheduler did not respond within 20 seconds") from exc


def state(runner=subprocess.run) -> dict:
    printed = _run(["launchctl", "print", f"{_domain()}/{LABEL}"], runner)
    disabled = _run(["launchctl", "print-disabled", _domain()], runner)
    loaded = printed.returncode == 0
    return {"loaded": loaded,
            "running": loaded and "state = running" in printed.stdout,
            "paused": f'"{LABEL}" => disabled' in disabled.stdout}


def pause(runner=subprocess.run) -> dict:
    if state(runner)["running"]:
        raise ControlError("a run is in progress; pause after it finishes")
    _run(["launchctl", "disable", f"{_domain()}/{LABEL}"], runner)
    _run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], runner)       # not loaded is fine
    return state(runner)


def resume(runner=subprocess.run) -> dict:
    _run(["launchctl", "enable", f"{_domain()}/{LABEL}"], runner)
    _run(["launchctl", "bootstrap", _domain(), str(PLIST)], runner)       # already loaded is fine
    return state(runner)


def run_now(runner=subprocess.run) -> dict:
    st = state(runner)
    if st["paused"] or not st["loaded"]:
        raise ControlError("the nightly job is paused; resume it first")
    if st["running"]:
        raise ControlError("a run is already in progress")
    r = _run(["launchctl", "kickstart", f"{_domain()}/{LABEL}"], runner)
    if r.returncode != 0:
        raise ControlError(f"launchctl kickstart failed: {(r.stderr or r.stdout).strip()[:200]}")
    return state(runner)


ACTIONS = {"pause": pause, "resume": resume, "run_now": run_now}
