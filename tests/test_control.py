"""Pause / resume / run now against a fake launchctl: fixed argv, guarded transitions."""

from __future__ import annotations

import subprocess

import pytest

from rem import control


class FakeLaunchctl:
    def __init__(self, loaded=True, running=False, disabled=False):
        self.loaded, self.running, self.disabled = loaded, running, disabled
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        assert argv[0] == "launchctl" and isinstance(argv, list)
        self.calls.append(argv[1:])
        cmd, out, code = argv[1], "", 0
        if cmd == "print":
            code = 0 if self.loaded else 113
            out = "state = running" if self.running else "state = not running"
        elif cmd == "print-disabled":
            out = f'"{control.LABEL}" => {"disabled" if self.disabled else "enabled"}'
        elif cmd == "disable":
            self.disabled = True
        elif cmd == "enable":
            self.disabled = False
        elif cmd == "bootout":
            self.loaded = False
        elif cmd == "bootstrap":
            self.loaded = True
        elif cmd == "kickstart":
            self.running = True
        return subprocess.CompletedProcess(argv, code, out, "")


def test_state_reads_loaded_running_paused():
    assert control.state(FakeLaunchctl(running=True)) == \
        {"loaded": True, "running": True, "paused": False}
    assert control.state(FakeLaunchctl(loaded=False, disabled=True)) == \
        {"loaded": False, "running": False, "paused": True}


def test_pause_then_resume_round_trips():
    fake = FakeLaunchctl()
    assert control.pause(fake) == {"loaded": False, "running": False, "paused": True}
    assert control.resume(fake) == {"loaded": True, "running": False, "paused": False}
    verbs = [c[0] for c in fake.calls if c[0] not in ("print", "print-disabled")]
    assert verbs == ["disable", "bootout", "enable", "bootstrap"]


def test_pause_refuses_during_a_run():
    with pytest.raises(control.ControlError, match="in progress"):
        control.pause(FakeLaunchctl(running=True))


def test_run_now_refuses_while_running_or_paused():
    with pytest.raises(control.ControlError, match="already in progress"):
        control.run_now(FakeLaunchctl(running=True))
    with pytest.raises(control.ControlError, match="paused"):
        control.run_now(FakeLaunchctl(loaded=False, disabled=True))


def test_run_now_kickstarts():
    fake = FakeLaunchctl()
    assert control.run_now(fake)["running"] is True
    assert ["kickstart", f"gui/{control.os.getuid()}/{control.LABEL}"] in fake.calls


def test_missing_scheduler_reports_manual_alternative():
    def missing(*args, **kwargs):
        raise FileNotFoundError("launchctl")

    with pytest.raises(control.ControlError, match="rem night manually"):
        control.state(missing)


def test_scheduler_timeout_is_a_control_error():
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(control.ControlError, match="did not respond"):
        control.run_now(timeout)


def test_configured_job_does_not_control_default_job(monkeypatch, tmp_path):
    import importlib

    with monkeypatch.context() as env:
        env.setenv("CONSOLIDATE_JOB_LABEL", "example.rem")
        env.setenv("CONSOLIDATE_JOB_PLIST", str(tmp_path / "custom.plist"))
        importlib.reload(control)
        fake = FakeLaunchctl(loaded=False)
        try:
            control.resume(fake)
            assert ["bootstrap", f"gui/{control.os.getuid()}",
                    str(tmp_path / "custom.plist")] in fake.calls
            assert ["enable", f"gui/{control.os.getuid()}/example.rem"] in fake.calls
            assert all("org.agent-rem.night" not in str(call) for call in fake.calls)
        finally:
            env.undo()
            importlib.reload(control)
