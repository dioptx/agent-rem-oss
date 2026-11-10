"""Render the briefing as a self-contained HTML view — the rolling visual surface.

Minimal style: white, verdict-first, color only for state, mono numbers.
Shows the original
"wake-up line + colored dots" timeline, per-pattern cards with Day-N escalation
badges, and the "since last review" gap summary. Read-only: acting still happens
in the markdown or via `rem review` (a click-to-save endpoint is a small
follow-up).
"""

from __future__ import annotations

import argparse
import html
from datetime import date
from pathlib import Path

from . import config
from .actions import recur_note
from .db import ConsolidationDB
from .report import _since_review_line

CSS = """
:root{--bg:#fafafa;--card:#fff;--border:#e5e7eb;--text:#0f172a;--muted:#64748b;
--green:#15803d;--green-soft:#ecfdf5;--red:#b91c1c;--red-soft:#fef2f2;
--amber:#b45309;--amber-soft:#fffbeb;--blue:#1e40af;--blue-soft:#eff6ff;
--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}body{background:var(--bg);color:var(--text);margin:0;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:24px}
main{max-width:860px;margin:0 auto}h1{font-size:20px;margin:0 0 2px}
.sub{color:var(--muted);font:13px/1.4 var(--mono)}
.gap{margin:10px 0 0;padding:8px 12px;background:var(--amber-soft);
border:1px solid var(--border);border-radius:8px;font:13px var(--mono);color:var(--amber)}
.legend{color:var(--muted);font-size:12px;margin:14px 0 18px;line-height:1.6}
.legend code{background:#f1f5f9;padding:1px 5px;border-radius:4px}
.timeline{display:flex;align-items:center;gap:6px;margin:14px 0 22px;padding:12px;
background:var(--card);border:1px solid var(--border);border-radius:10px;overflow-x:auto}
.tl-label{font:11px var(--mono);color:var(--muted);white-space:nowrap;margin-right:4px}
.dot{height:14px;border-radius:7px;flex:none}
.card{background:var(--card);border:1px solid var(--border);border-left-width:4px;
border-radius:10px;padding:14px 16px;margin:0 0 14px}
.card.drag{border-left-color:var(--red)}.card.lift{border-left-color:var(--green)}
.chead{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.pos{font:13px var(--mono);color:var(--muted)}
.title{font-weight:600;font-size:15px}
.chip{font:10px var(--mono);text-transform:uppercase;letter-spacing:.06em;
padding:2px 7px;border-radius:10px;border:1px solid var(--border);color:var(--muted)}
.chip.drag{background:var(--red-soft);color:var(--red)}
.chip.lift{background:var(--green-soft);color:var(--green)}
.metric{font:12px var(--mono);color:var(--muted)}
.badge{font:10px var(--mono);text-transform:uppercase;letter-spacing:.06em;
padding:2px 7px;border-radius:10px;background:var(--amber-soft);color:var(--amber);
border:1px solid #fde68a}
.badge.hot{background:var(--red-soft);color:var(--red);border-color:#fecaca}
.layer{margin:6px 0;font-size:14px}.layer b{color:var(--muted);font-weight:600}
.props{margin:8px 0 0;padding:0;list-style:none;font-size:13px}
.props li{padding:3px 0}.tier{font:11px var(--mono);color:var(--blue);
background:var(--blue-soft);padding:1px 6px;border-radius:4px;margin-right:6px}
.empty{color:var(--muted);font-style:italic}
"""


def _esc(s) -> str:
    return html.escape(str(s or ""))


def render_html(db: ConsolidationDB, run_date: str) -> str:
    briefs = db.briefs_for(run_date)
    run = db.conn.execute(
        "SELECT * FROM runs WHERE run_date=? ORDER BY id DESC LIMIT 1", (run_date,)
    ).fetchone()
    import json
    window = run["window_days"] if run and run["window_days"] is not None else config.DEFAULT_WINDOW
    sessions = run["sessions_scanned"] if run else 0
    nproj = len(json.loads(run["projects"])) if run and run["projects"] else 0
    pats = [db.get_pattern(b["pattern_id"]) for b in briefs]
    ndrag = sum(1 for p in pats if p and p["valence"] == "drag")
    nlift = sum(1 for p in pats if p and p["valence"] == "lift")
    since = _since_review_line(db, run_date, pats).strip("_")

    # timeline: one colored dot per surfaced pattern, width scaled by score
    max_score = max((p["weighted_score"] for p in pats if p), default=1) or 1
    dots = []
    for p in pats:
        if not p:
            continue
        w = 16 + int(60 * p["weighted_score"] / max_score)
        color = "var(--green)" if p["valence"] == "lift" else "var(--red)"
        dots.append(f'<div class="dot" style="width:{w}px;background:{color};'
                    f'opacity:.85" title="{_esc(p["title"])}"></div>')

    cards = []
    for b in briefs:
        p = db.get_pattern(b["pattern_id"])
        if not p:
            continue
        streak = p.get("surfaced_streak") or 0
        badge = ""
        if streak >= 1:
            cls = "badge hot" if streak >= 3 else "badge"
            badge = f'<span class="{cls}">Day {streak + 1} · unresolved</span>'
        if p.get("prior_decision"):     # accepted/dropped and it came back
            badge += f'<span class="badge hot">{_esc(recur_note(p))}</span>'
        props = "".join(
            f'<li><span class="tier">T{pr["tier"]}</span>{_esc(pr["text"])}</li>'
            for pr in db.proposals_for(p["id"], run_date))
        if not props:
            props = '<li class="empty">no proposal generated</li>'
        snapshot = json.loads(b.get("evidence_json") or "null")
        if snapshot:
            evidence = (f"<details><summary>Evidence: {snapshot['event_groups']} event groups across "
                        f"{snapshot['sessions']} known sessions</summary>"
                        f"<p>{_esc(snapshot['window_start'])} to {_esc(snapshot['window_end'])}</p>")
            for event in snapshot["events"]:
                evidence += (f"<p><code>{_esc(event['id'])}</code> · {_esc(event['occurred_date'])} · "
                             f"{_esc(event.get('agent') or 'unknown agent')} · session "
                             f"<code>{_esc(event.get('session_id') or 'unknown')}</code></p>")
                evidence += "".join(f"<blockquote>{_esc(q)}</blockquote>" for q in event["quotes"])
            evidence += "</details>"
        else:
            evidence = '<p class="empty">No source snapshot was recorded for this briefing.</p>'
        cards.append(f"""<div class="card {_esc(p['valence'])}">
  <div class="chead"><span class="pos">{b['position']}</span>
    <span class="chip {_esc(p['valence'])}">{_esc(p['valence'])}</span>
    <span class="chip">{_esc(p['axis'])}</span>
    <span class="title">{_esc(p['title'])}</span>{badge}
    <span class="metric">{p['occurrence_count']} event groups · score {p['weighted_score']}</span>
  </div>
  <div class="layer"><b>Event</b> — {_esc(b['event_line'])}</div>
  <div class="layer"><b>Pattern</b> — {_esc(b['pattern_line'])}</div>
  {evidence}
  <ul class="props">{props}</ul>
</div>""")

    gap = f'<div class="gap">{_esc(since)}</div>' if since else ""
    empty = ("Nothing recurred above the bar in the observed sessions." if sessions else
             "No sessions were observed. Check the configured transcript folders, then run REM after a coding session.")
    body = "".join(cards) or f'<p class="empty">{empty}</p>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Night consolidation — {run_date}</title><style>{CSS}</style></head><body><main>
<h1>Night consolidation</h1>
<div class="sub">{run_date} · {sessions} sessions / {nproj} projects · {len(briefs)} patterns · {ndrag} drag · {nlift} lift · window {window}d</div>
{gap}
<div class="timeline"><span class="tl-label">wake →</span>{''.join(dots)}</div>
<div class="legend">Check a box / <code>snooze Nd</code> / <code>note:</code> / <code>drop</code> (this instance) / <code>mute</code> (silence for good) in the markdown, then run <code>rem review</code>. Blank carries over and climbs. Score weights recent + concrete signals highest (yesterday ×3; a tool error or refusal &gt; a keyword correction). A future agent acts on accepted items — this view doesn't.</div>
{body}
</main></body></html>"""


def write_html(db: ConsolidationDB, run_date: str,
               journal_dir: Path | None = None) -> Path:
    journal_dir = journal_dir or config.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    out = journal_dir / f"journal-{run_date}.html"
    out.write_text(render_html(db, run_date), encoding="utf-8")
    (journal_dir / "latest.html").write_text(out.read_text(encoding="utf-8"),
                                             encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the HTML briefing view.")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--db", default=None)
    ap.add_argument("--journal-dir", default=None)
    args = ap.parse_args()
    db = ConsolidationDB(Path(args.db) if args.db else None)
    out = write_html(db, args.today,
                     Path(args.journal_dir) if args.journal_dir else None)
    print(f"wrote {out}")
    db.close()


if __name__ == "__main__":
    main()
