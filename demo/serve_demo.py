#!/usr/bin/env python3
"""Run REM's real page with an isolated demo context and inert scheduler controls."""
import argparse
import json
from pathlib import Path
import subprocess


def demo_runner(argv, **_kwargs):
    if argv[1] == "print-disabled":
        return subprocess.CompletedProcess(argv, 0, "{}", "")
    if argv[1] == "print":
        return subprocess.CompletedProcess(argv, 1, "", "No demo job is installed.")
    raise ValueError("Demo scheduler controls are disabled; this is synthetic data.")


def demo_context(folder):
    from rem.serve import Ctx
    folder = Path(folder).resolve()
    anchor = json.loads((folder / "demo.json").read_text())["today"]
    return Ctx(db_path=folder / "events.db", journal_dir=folder / "journals",
               settings_path=folder / "settings.json", heartbeat=folder / "history.jsonl",
               runner=demo_runner, today=lambda: anchor, tailnet_host=None, tailnet_login=None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--port", type=int, default=3043)
    args = parser.parse_args()
    from rem.serve import make_server
    server = make_server(args.port, demo_context(args.folder))
    print(f"Synthetic REM demo: http://127.0.0.1:{server.server_address[1]}", flush=True)
    print("Scheduler buttons are disabled. Triage and settings affect only this demo.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
