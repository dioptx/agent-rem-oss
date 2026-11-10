"""Triage a briefing from Telegram instead of the laptop.

`push` sends each surfaced pattern of a journal as one Telegram question (tiers to accept,
plus Snooze 7d / Drop / Mute; a text reply becomes a note). `collect` picks up the taps and
writes them into the journal markdown exactly as a hand edit would, then runs `rem review`,
so verdicts go through the same parser as always.

The optional Telegram side needs a spool-and-relay client (one poller per bot token).
Set REM_ASK_CMD to it, or set REM_REMOTE_HOST to run `rem-ask` on that host.
No remote transport runs when neither setting is present.

  python3 -m rem.telegram push [--journal PATH]
  python3 -m rem.telegram collect
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import config

_REMOTE_HOST = os.environ.get('REM_REMOTE_HOST', '')
ASK_CMD = os.environ.get('REM_ASK_CMD') or (
    f'ssh -o ConnectTimeout=8 {shlex.quote(_REMOTE_HOST)} '
    'rem-ask'
    if _REMOTE_HOST else '')
TITLE = "🌙 agent-rem"
EXTRA = ("Snooze 7d", "Drop", "Mute")

HEAD_RE = re.compile(r"^## \d+ (▼|▲) (\w+) · (\w+) · (.+?) · (?:×\d+|\d+ event groups?).*<!--P:(\d+)-->")
PROP_RE = re.compile(r"^- \[( |x|X)\]\s+T(\d)\s+—\s+(.*?)\s*<!--p:(\d+)-->")


def parse_journal(text: str) -> list[dict]:
    """Surfaced patterns in a journal: id, title, valence, event, proposals (tier, text, id)."""
    items, cur = [], None
    for line in text.splitlines():
        m = HEAD_RE.match(line)
        if m:
            cur = {"pattern_id": int(m.group(5)), "valence": m.group(2).lower(), "axis": m.group(3),
                   "title": m.group(4), "event": "", "proposals": []}
            items.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("**Event**"):
            cur["event"] = line.split("—", 1)[-1].strip()
        p = PROP_RE.match(line)
        if p:
            cur["proposals"].append({"tier": int(p.group(2)), "text": p.group(3), "id": int(p.group(4))})
    return items


def label(tier: int, text: str) -> str:
    # multi-select answers come back comma-joined, so labels carry no commas
    return f"T{tier} · {text.replace(',', ';')}"


def question(item: dict) -> dict:
    arrow = "▲ LIFT" if item["valence"] == "lift" else "▼ DRAG"
    opts = [{"label": label(p["tier"], p["text"])} for p in item["proposals"]]
    opts += [{"label": x} for x in EXTRA]
    return {"question": f"{arrow} · {item['title']}\n{item['event']}", "header": "agent-rem",
            "multiSelect": True, "options": opts}


def apply_answer(md: str, pattern_id: int, answer: str) -> str:
    """Write one Telegram answer into the journal the way a hand edit would."""
    tiers = {int(t) for t in re.findall(r"(?:^|, )T(\d) · ", answer)}
    flags = [x for x in EXTRA if re.search(rf"(?:^|, ){re.escape(x)}(?:,|$)", answer)]
    out, inside = [], False
    for line in md.splitlines():
        if HEAD_RE.match(line):
            inside = f"<!--P:{pattern_id}-->" in line
        elif line.startswith("## ") or line.strip() == "---":
            inside = False
        if inside:
            p = PROP_RE.match(line)
            if p and int(p.group(2)) in tiers:
                line = line.replace("- [ ]", "- [x]", 1)
            elif line.strip() == "note:":
                if flags:
                    line = "  " + {"Snooze 7d": "snooze 7d", "Drop": "drop", "Mute": "mute"}[flags[0]]
                elif not tiers:
                    line = f"  note: {answer.strip()}"
        out.append(line)
    return "\n".join(out) + ("\n" if md.endswith("\n") else "")


def _ask(args: list[str], stdin: str | None = None) -> str:
    if not ASK_CMD:
        raise RuntimeError('Telegram transport is off; set REM_REMOTE_HOST or REM_ASK_CMD')
    cmd = shlex.split(ASK_CMD)
    # over ssh the remote command is one string; locally it is argv
    cmd = cmd[:-1] + [cmd[-1] + " " + shlex.join(args)] if cmd[0] == "ssh" else cmd + args
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ask transport failed rc={r.returncode}: {r.stderr[-300:]}")
    return r.stdout


def latest_journal() -> Path | None:
    files = sorted(config.JOURNAL_DIR.glob("journal-*.md"))
    return files[-1] if files else None


def push(journal: Path) -> int:
    if not ASK_CMD:
        return 0
    items = parse_journal(journal.read_text(encoding="utf-8"))
    for item in items:
        payload = {"kind": "rem", "title": TITLE, "where": journal.name,
                   "ref": {"pattern_id": item["pattern_id"], "journal": str(journal)},
                   "questions": [question(item)]}
        _ask(["post"], json.dumps(payload, ensure_ascii=False))
    return len(items)


def collect() -> int:
    if not ASK_CMD:
        return 0
    done = json.loads(_ask(["collect", "--kind", "rem", "--max-age-days", "8"]) or "[]")
    for d in done:
        ref = d.get("ref") or {}
        path = Path(ref.get("journal", ""))
        if not path.exists():
            print(f"skip: journal gone for pattern {ref.get('pattern_id')}", file=sys.stderr)
            continue
        path.write_text(apply_answer(path.read_text(encoding="utf-8"), int(ref["pattern_id"]), d["answers"][0]),
                        encoding="utf-8")
    if done:
        subprocess.run([sys.executable, "-m", "rem.review"], cwd=config.REPO_DIR, check=False)
    return len(done)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["push", "collect"])
    ap.add_argument("--journal", default=None)
    a = ap.parse_args()
    if a.cmd == "push":
        j = Path(a.journal) if a.journal else latest_journal()
        if not j:
            print("no journal to push")
            return
        print(f"pushed {push(j)} pattern(s) from {j.name} to Telegram")
    else:
        print(f"collected {collect()} Telegram answer(s)")


if __name__ == "__main__":
    main()
