"""rem serve: the live REM analytics page and control panel. It binds 127.0.0.1 only.

  python3 -m rem.serve [--port 3043]

Remote access goes through `tailscale serve`, which proxies to 127.0.0.1 and stamps each request
with the caller's Tailscale-User-Login. A proxied request (X-Forwarded-For or that header set)
must name REM_TAILNET_HOST as its host and REM_TAILNET_LOGIN as its user; with either unset,
proxied requests are refused.

Every GET reads the DB fresh. Every POST needs header X-REM: 1 and this server's own Origin; a
cross-site form can't set a custom header without a CORS preflight, which this server never
answers, so other browser tabs can't trigger writes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import config, control, feedback, settings, stats, triage
from .db import ConsolidationDB
from .review import _describe, latest_journal

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024
DEFAULT_PORT = int(os.environ.get("REM_SERVE_PORT", "3043"))
_WRITE_LOCK = threading.Lock()


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _today() -> str:
    return date.today().isoformat()


@dataclass
class Ctx:
    db_path: Path = config.DB_PATH
    journal_dir: Path = config.JOURNAL_DIR
    settings_path: Path = settings.SETTINGS_PATH
    heartbeat: Path = stats.HEARTBEAT
    runner: Callable = subprocess.run
    today: Callable[[], str] = _today
    tailnet_host: str | None = os.environ.get("REM_TAILNET_HOST") or None
    tailnet_login: str | None = os.environ.get("REM_TAILNET_LOGIN") or None
    present: bool = os.environ.get("REM_PRESENT") == "1"


PRESENT_HIDDEN = "Details stay on this machine while presenting."


def scrub_overview(o: dict) -> dict:
    """Presenter mode: drop session text, log lines and links before they leave the server, so a
    projector or devtools never shows a path, a repo or a quote (page audit, 2026-10-27)."""
    o = {**o, "present": True}
    o["shift"] = {**o.get("shift", {}), "log": None}
    o["accepted"] = [{**a, "ideas": [], "implementations": [], "note": "", "fix_pr": None,
                      "made": bool(a.get("fix_pr"))}
                     for a in o.get("accepted", [])]
    o["fixes"] = [{**f, "fix_pr": None} for f in o.get("fixes", [])]
    # night recaps are model-written and can name repos
    o["runs"] = [{**r, "summary": ""} for r in o.get("runs", [])]
    tiles = o.get("tiles", {})
    if tiles.get("runs", {}).get("last"):
        o["tiles"] = {**tiles, "runs": {**tiles["runs"], "last": {**tiles["runs"]["last"], "summary": ""}}}
    return o


def scrub_triage(items: list[dict]) -> list[dict]:
    return [{**it, "event": PRESENT_HIDDEN, "pattern": "", "recommend_why": "",
             "proposals": [{**p, "text": f"Fix idea {i + 1}"} for i, p in enumerate(it.get("proposals", []))]}
            for it in items]


class Handler(BaseHTTPRequestHandler):
    server_version = "rem-serve"
    timeout = 10

    @property
    def ctx(self) -> Ctx:
        return self.server.ctx

    # ---- plumbing ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, default=str).encode())

    def _via_tailnet(self) -> bool:
        return bool(self.headers.get("X-Forwarded-For") or self.headers.get("Tailscale-User-Login"))

    def _allowed(self) -> bool:
        port = self.server.server_address[1]
        origins = ((f"https://{self.ctx.tailnet_host}",) if self._via_tailnet()
                   else (f"http://127.0.0.1:{port}", f"http://localhost:{port}"))
        return self.headers.get("X-REM") == "1" and self.headers.get("Origin", "") in origins

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        if self._via_tailnet():
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
            ok = (self.ctx.tailnet_host is not None and self.ctx.tailnet_login is not None
                  and host == self.ctx.tailnet_host
                  and self.headers.get("Tailscale-User-Login") == self.ctx.tailnet_login)
        else:
            ok = self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")
        if ok:
            return True
        self._json(403, {"ok": False, "error": "forbidden host"})
        return False

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length") from None
        if n < 0:
            raise ValueError("bad Content-Length")
        if n > MAX_BODY:
            raise ValueError("request too large")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _dispatch(self, fn, *args) -> None:
        try:
            fn(*args)
        except (ValueError, control.ControlError) as e:
            self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:                      # keep serving; the page shows the message
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"ok": False, "error": f"server error: {e}"})

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        routes = {"/": self._page, "/health": self._health, "/api/overview": self._overview,
                  "/api/settings": self._settings, "/api/control": self._job,
                  "/api/triage": self._triage}
        fn = routes.get(self.path.split("?")[0])
        if fn is None:
            return self._json(404, {"ok": False, "error": "not found"})
        self._dispatch(fn)

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        routes = {"/api/settings": self._save_settings, "/api/control": self._control,
                  "/api/rem-triage": self._save_triage}
        fn = routes.get(self.path)
        if fn is None:
            return self._json(404, {"ok": False, "error": "not found"})
        if self.ctx.present:   # a demo click must never save a call, a setting or start a shift
            return self._json(403, {"ok": False, "error": "presenting, so changes are off"})
        if not self._allowed():
            return self._json(403, {"ok": False, "error": "forbidden"})
        try:
            body = self._body()
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        with _WRITE_LOCK:
            self._dispatch(fn, body)

    # ---- reads ------------------------------------------------------------
    def _page(self) -> None:
        self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

    def _health(self) -> None:
        self._json(200, {"ok": True})

    def _overview(self) -> None:
        conn = stats.connect(self.ctx.db_path)
        try:
            o = stats.overview(conn, self.ctx.today(), self.ctx.heartbeat)
            self._json(200, {"ok": True, **(scrub_overview(o) if self.ctx.present else o)})
        finally:
            conn.close()

    def _settings(self) -> None:
        self._json(200, {"ok": True, "settings": settings.effective(self.ctx.settings_path),
                         "modes": settings.modes(self.ctx.settings_path)})

    def _job(self) -> None:
        self._json(200, {"ok": True, "job": control.state(self.ctx.runner)})

    def _latest(self) -> tuple[str | None, Path | None]:
        path = latest_journal(Path(self.ctx.journal_dir))
        return (path.stem.removeprefix("journal-"), path) if path else (None, None)

    def _logged(self, path: Path) -> dict[int, str]:
        """Verdicts already marked in the journal, by pattern (carries excluded)."""
        return {v.pattern_id: _describe(v) for v in feedback.parse_journal(path.read_text("utf-8"))
                if v.decision != "carry"}

    def _triage(self) -> None:
        run_date, path = self._latest()
        items = []
        if run_date:
            db = ConsolidationDB(self.ctx.db_path)
            try:
                items = triage.cards(db, run_date)
            finally:
                db.close()
            logged = self._logged(path)
            for it in items:
                it["logged"] = logged.get(it["pattern_id"])
        if self.ctx.present:
            items = scrub_triage(items)
        self._json(200, {"ok": True, "run_date": run_date, "items": items, "present": self.ctx.present})

    # ---- writes -----------------------------------------------------------
    def _save_settings(self, body: dict) -> None:
        db = ConsolidationDB(self.ctx.db_path)
        try:
            changed = []
            if body.get("modes"):
                changed += settings.save_modes(body["modes"], path=self.ctx.settings_path,
                                               db=db, source="ui")
            if body.get("changes") or not body.get("modes"):
                changed += settings.save(body.get("changes") or {}, path=self.ctx.settings_path,
                                         db=db, source="ui")
        finally:
            db.close()
        self._json(200, {"ok": True,
                         "changed": [{"key": k, "old": o, "new": n} for k, o, n in changed]})

    def _control(self, body: dict) -> None:
        action = body.get("action")
        if action not in control.ACTIONS:
            raise ValueError(f"unknown action: {action}")
        job = control.ACTIONS[action](self.ctx.runner)
        db = ConsolidationDB(self.ctx.db_path)
        try:
            db.log_setting("nightly job", None, action, "ui")
        finally:
            db.close()
        self._json(200, {"ok": True, "job": job})

    def _save_triage(self, body: dict) -> None:
        run_date, path = self._latest()
        if not path or body.get("run_date") != run_date:
            raise ValueError("only the newest briefing can be triaged here; "
                             "edit older journals by hand")
        items = body.get("items", [])
        if not isinstance(items, list) or not all(isinstance(it, dict) for it in items):
            raise ValueError("items must be a list of objects")
        for it in items:
            accept = it.get("accept", [])
            if not _is_int(it.get("pattern_id")):
                raise ValueError("pattern_id must be an integer")
            if not isinstance(accept, list) or not all(_is_int(t) for t in accept):
                raise ValueError("accept must be a list of integers")
            if "action" in it and not isinstance(it["action"], str):
                raise ValueError("action must be a string")
        already = self._logged(path)
        md = path.read_text(encoding="utf-8")
        skipped = []
        for it in items:
            pid = it["pattern_id"]
            if pid in already:                       # marks can be added, never undone here
                skipped.append(pid)
                continue
            action = it.get("action") if it.get("action") in triage.ACTIONS else "accept"
            md = triage.apply_marks(md, pid, it.get("accept", []), action,
                                    str(it.get("note", ""))[:500])
        path.write_text(md, encoding="utf-8")
        verdicts = feedback.parse_journal(md)
        db = ConsolidationDB(self.ctx.db_path)
        try:
            feedback.apply(db, verdicts, run_date, self.ctx.today(), bump_carries=False)
            logged = []
            for v in verdicts:
                if v.decision == "carry" or v.pattern_id in already:
                    continue
                p = db.get_pattern(v.pattern_id)
                logged.append({"title": p["title"] if p else f"#{v.pattern_id}",
                               "verdict": _describe(v)})
        finally:
            db.close()
        self._json(200, {"ok": True, "logged": logged, "skipped": skipped})


def make_server(port: int = DEFAULT_PORT, ctx: Ctx | None = None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.ctx = ctx or Ctx()
    return srv


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the REM analytics page on 127.0.0.1.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--present", action="store_true",
                    help="presenter mode: read-only, no session text, logs or links in the page")
    args = ap.parse_args()
    ConsolidationDB(config.DB_PATH).close()   # migrate once so read-only queries find every table
    srv = make_server(args.port, Ctx(present=args.present or Ctx().present))
    print(f"rem serve on http://127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
