"""Triage a briefing on a phone-friendly page instead of editing markdown.

`page` renders one card per surfaced pattern: the proposals as checkboxes with the model's
recommended tier pre-ticked, an action (accept checked / snooze 7d / drop / mute) and a note.
Save posts the choices to the configured report server (POST /api/rem-triage), which spools
them. `collect` fetches that spool, writes the choices into the journal exactly as a hand edit
would, and runs `rem review` so verdicts go through the one parser.

  python3 -m rem.triage page [--today D] [--out PATH]
  python3 -m rem.triage collect
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import config, feedback
from .db import ConsolidationDB
from .telegram import HEAD_RE, PROP_RE

_REMOTE_HOST = os.environ.get('REM_REMOTE_HOST', '')
FETCH_CMD = os.environ.get('REM_TRIAGE_FETCH') or (
    f'ssh -o ConnectTimeout=8 {shlex.quote(_REMOTE_HOST)} python3 -' if _REMOTE_HOST else '')
FETCH_SCRIPT = r'''
import json, os, glob, shutil
d = os.path.expanduser("~/.local/share/agent-rem-triage")
os.makedirs(os.path.join(d, "done"), exist_ok=True)
out = []
for f in sorted(glob.glob(os.path.join(d, "*.json"))):
    try:
        out.append(json.load(open(f)))
    except ValueError:
        pass
    shutil.move(f, os.path.join(d, "done", os.path.basename(f)))
print(json.dumps(out))
'''
ACTIONS = ("accept", "snooze", "drop", "mute")


def clean(s: str) -> str:
    return (s or "").replace("—", ", ").replace("–", "-")


# ---- page -----------------------------------------------------------------
def cards(db: ConsolidationDB, run_date: str) -> list[dict]:
    out = []
    for b in db.briefs_for(run_date):
        pid = b["pattern_id"]
        pat = db.conn.execute("SELECT * FROM patterns WHERE id=?", (pid,)).fetchone()
        props = db.proposals_for(pid, run_date)
        rec = b.get("recommend_tier")
        why = b.get("recommend_why") or ""
        if rec is None and props:           # briefs saved before recommendations existed
            rec, why = 1, "smallest change; saved before the model gave recommendations"
        out.append({"pattern_id": pid, "title": pat["title"] if pat else f"pattern {pid}",
                    "valence": (pat["valence"] if pat else "drag") or "drag",
                    "axis": pat["axis"] if pat else "", "count": pat["occurrence_count"] if pat else 0,
                    "event": b.get("event_line") or "", "pattern": b.get("pattern_line") or "",
                    "proposals": [{"tier": r["tier"], "text": r["text"]} for r in props],
                    "recommend_tier": rec, "recommend_why": why})
    return out


CSS = """:root{--ground:#FFF;--text:#16181D;--muted:#6B7280;--accent:#6D28D9;--soft:#F5F3FF;--line:#E7E9EE;--chip:#F4F6F9;
--drag:#B45309;--lift:#15803D;--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}html{overflow-x:hidden}
body{background:var(--ground);color:var(--text);font-family:var(--sans);line-height:1.5;margin:0;overflow-wrap:break-word}
.wrap{max-width:760px;margin:0 auto;padding:28px 16px 120px}
h1{font-size:24px;margin:0 0 6px}.sub{color:var(--muted);font-size:14px;margin:0 0 20px}
.card{border:1px solid var(--line);border-radius:12px;padding:14px 14px 10px;margin:0 0 14px}
.kind{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.kind.drag{color:var(--drag)}.kind.lift{color:var(--lift)}
.t{font-size:17px;font-weight:700;margin:4px 0 6px}.ev{font-size:14px;color:#33373F;margin:0 0 4px}
.pt{font-size:13px;color:var(--muted);margin:0 0 10px}
label.opt{display:flex;gap:10px;align-items:flex-start;padding:10px;border:1px solid var(--line);border-radius:10px;margin:0 0 8px;font-size:14.5px}
label.opt input{width:20px;height:20px;margin-top:1px;flex:none}
.rec{display:inline-block;font-family:var(--mono);font-size:10.5px;font-weight:700;background:var(--soft);color:var(--accent);
padding:2px 7px;border-radius:20px;margin-left:4px}
.why{font-size:12.5px;color:var(--accent);margin:-2px 0 8px 2px}
.acts{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 8px}
.acts label{font-size:13.5px;border:1px solid var(--line);border-radius:20px;padding:6px 12px}
.acts input{margin-right:6px}
textarea{width:100%;min-height:44px;border:1px solid var(--line);border-radius:10px;padding:8px;font:inherit;font-size:14px}
.bar{position:fixed;left:0;right:0;bottom:0;background:#FFFFFFF2;border-top:1px solid var(--line);padding:12px 16px}
.bar .in{max-width:760px;margin:0 auto;display:flex;gap:12px;align-items:center}
button{background:var(--accent);color:#FFF;border:0;border-radius:10px;padding:12px 18px;font-size:16px;font-weight:700}
#msg{font-size:13.5px;color:var(--muted)}"""

JS = """
function collect(){
  var items=[];
  document.querySelectorAll('.card').forEach(function(c){
    var pid=+c.dataset.pid, acc=[];
    c.querySelectorAll('input[type=checkbox]:checked').forEach(function(b){acc.push(+b.value)});
    var a=c.querySelector('input[type=radio]:checked'); var note=c.querySelector('textarea').value.trim();
    items.push({pattern_id:pid, accept:acc, action:a?a.value:'accept', note:note});
  });
  return {run_date:document.body.dataset.run, items:items};
}
function save(){
  var m=document.getElementById('msg'), body=collect();
  m.textContent='Saving...';
  fetch('/api/rem-triage',{method:'POST',headers:{'Content-Type':'application/json','X-REM':'1'},body:JSON.stringify(body)})
   .then(function(r){return r.json()}).then(function(j){
     var n={accept:0,snooze:0,drop:0,mute:0}; body.items.forEach(function(i){
       if(i.action==='accept'&&!i.accept.length) return; n[i.action]++;});
     m.textContent=j.ok?('Saved: '+n.accept+' accepted, '+n.snooze+' snoozed, '+n.drop+' dropped, '+n.mute+' muted. Applied within the hour.'):('Not saved: '+(j.error||'server error'));
   }).catch(function(e){m.textContent='Not saved ('+e+'). Copy this and send it to Claude: '+JSON.stringify(body)});
}
"""


def render_page(run_date: str, items: list[dict]) -> str:
    e = lambda s: html.escape(clean(str(s)))  # noqa: E731
    parts = []
    for it in items:
        pid, rec = it["pattern_id"], it["recommend_tier"]
        opts = "".join(
            f"<label class='opt'><input type='checkbox' value='{p['tier']}'{' checked' if p['tier'] == rec else ''}>"
            f"<span><b>T{p['tier']}</b> {e(p['text'])}"
            + ("<span class='rec'>RECOMMENDED</span>" if p["tier"] == rec else "") + "</span></label>"
            for p in it["proposals"])
        why = f"<div class='why'>Why: {e(it['recommend_why'])}</div>" if it["recommend_why"] else ""
        default = "accept" if rec else "snooze"
        acts = "".join(
            f"<label><input type='radio' name='a{pid}' value='{a}'{' checked' if a == default else ''}>"
            f"{ {'accept': 'Accept ticked', 'snooze': 'Snooze 7d', 'drop': 'Drop', 'mute': 'Mute'}[a] }</label>"
            for a in ACTIONS)
        arrow = "▲ LIFT" if it["valence"] == "lift" else "▼ DRAG"
        parts.append(
            f"<div class='card' data-pid='{pid}'><div class='kind {e(it['valence'])}'>{arrow} · {e(it['axis'])} · x{e(it['count'])}</div>"
            f"<div class='t'>{e(it['title'])}</div><p class='ev'>{e(it['event'])}</p><p class='pt'>{e(it['pattern'])}</p>"
            f"{opts}{why}<div class='acts'>{acts}</div><textarea placeholder='Note (optional)'></textarea></div>")
    body = "".join(parts) or "<p>Nothing surfaced this week.</p>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'><title>Agent REM Triage</title>"
            f"<style>{CSS}</style></head><body data-run='{e(run_date)}'><div class='wrap'>"
            f"<h1>agent-rem triage · {e(run_date)}</h1><p class='sub'>{len(items)} patterns. The recommended fix is "
            f"ticked; change anything, then Save. REM records your choices and changes nothing else.</p>{body}</div>"
            f"<div class='bar'><div class='in'><button onclick='save()'>Save choices</button><span id='msg'></span></div></div>"
            f"<script>{JS}</script></body></html>")


# ---- marks ----------------------------------------------------------------
def apply_marks(md: str, pattern_id: int, tiers: list[int], action: str, note: str = "") -> str:
    """Write one pattern's choices into the journal the way a hand edit would."""
    note = " ".join((note or "").split()).replace("<!--", "").replace("-->", "")
    if note and (note.split()[0].lower() in ("drop", "dismiss", "reject", "mute", "silence")
                 or feedback.SNOOZE_RE.search(note)):
        note = "re: " + feedback.SNOOZE_RE.sub(lambda m: f"snooze for {m.group(1)} days", note)
    accept = action == "accept" and bool(tiers)
    line_for = {"snooze": "  snooze 7d", "drop": "  drop", "mute": "  mute"}
    out, inside = [], False
    for line in md.splitlines():
        if HEAD_RE.match(line):
            inside = f"<!--P:{pattern_id}-->" in line
        elif line.startswith("## ") or line.strip() == "---":
            inside = False
        if inside:
            p = PROP_RE.match(line)
            if p and accept and int(p.group(2)) in tiers:
                line = line.replace("- [ ]", "- [x]", 1)
            elif line.strip() == "note:":
                if action in line_for:
                    line = line_for[action] + (f"\n  note: {note}" if note else "")
                elif note:
                    line = f"  note: {note}"
        out.append(line)
    return "\n".join(out) + ("\n" if md.endswith("\n") else "")


def collect() -> int:
    from . import retro
    db = ConsolidationDB(config.DB_PATH)
    try:
        retro.collect(db, date.today().isoformat())
    finally:
        db.close()
    if not FETCH_CMD:
        return 0
    r = subprocess.run(shlex.split(FETCH_CMD), input=FETCH_SCRIPT, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"fetch failed rc={r.returncode}: {r.stderr[-300:]}")
    saves = json.loads(r.stdout or "[]")
    applied = 0
    for s in saves:
        path = config.JOURNAL_DIR / f"journal-{s.get('run_date', '')}.md"
        if not path.exists():
            print(f"skip: no journal for {s.get('run_date')}", file=sys.stderr)
            continue
        md = path.read_text(encoding="utf-8")
        for it in s.get("items", []):
            action = it.get("action") if it.get("action") in ACTIONS else "accept"
            md = apply_marks(md, int(it["pattern_id"]), [int(t) for t in it.get("accept", [])], action,
                             str(it.get("note", ""))[:500])
        path.write_text(md, encoding="utf-8")
        applied += 1
    if applied:
        subprocess.run([sys.executable, "-m", "rem.review"], cwd=config.REPO_DIR, check=False)
    return applied


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["page", "collect"])
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.cmd == "collect":
        print(f"applied {collect()} saved triage page(s)")
        return
    db = ConsolidationDB(config.DB_PATH)
    items = cards(db, a.today)
    out = Path(a.out) if a.out else config.JOURNAL_DIR / "triage.html"
    out.write_text(render_page(a.today, items), encoding="utf-8")
    print(f"{out} · {len(items)} patterns")


if __name__ == "__main__":
    main()
