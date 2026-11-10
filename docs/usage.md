# agent-rem

Mimics what the brain does during sleep: replays the last few days of Claude
Code and Codex sessions, consolidates friction and learnings into durable **events**,
notices which **recur**, and writes a tiny morning briefing you clear in under a
minute. It observes, reflects, proposes — and stops. It never fixes anything; a
later agent acts on what you accept.

Every event, pattern, proposal, and your verdict lives in SQLite so future
agents can re-count patterns across weeks.

## The three layers

Each surfaced item stacks three layers in the morning markdown:

1. **Event** — what was observed, with evidence (project, count, a quote).
2. **Pattern** — the recurrence reflection: it happened N× in the window, spiked yesterday.
3. **Proposals** — 1–3 *layered* directions (T1 tiny → T3 systemic), never full solutions.

Each item is tagged on four **axes** — `autonomy`, `convergence`, `friction`,
`introspection` — and a **valence**: `drag` (reduce) or `lift` (reinforce).

## Closing the loop

A briefing only matters if you act on it. The full cycle:

**observe → cluster → score → surface → triage → act → mark → track**

- **score** — recency (yesterday ×3) *and* per-kind severity: a real tool error or refusal outweighs a keyword "correction", so noise doesn't top the list (`config.KIND_SEVERITY`).
- **triage** — `rem review` reads your marks: check a box to **accept**, `mute` a whole signature for good, `drop` this instance, `snooze Nd`, or `note:` a comment. Blank carries over and climbs.
- **act** — every accept should be backed by a real edit; `rem actions --preview` turns each proposal into a paste-ready action bound to the project, session, and quote it came from.
- **track** — `rem ledger` is the ongoing record (runs, carrying patterns, decisions). An accepted signature that recurs reopens badged "recurred after you accepted it". This reports recurrence, not proof that a fix succeeded or failed.

After implementing an accepted proposal, record its effective date and scope:

```bash
rem actions --implemented PROPOSAL_ID --effective-on YYYY-MM-DD --scope TEXT --reference TEXT
```

`rem actions` shows proposal identifiers. Implementation records are explicitly
user-reported; acceptance, implementation and observed recurrence remain distinct.
REM does not grade causal effectiveness. The optional defect command can file
bug reports; REM does not implement the fixes.

The optional `rem-review-act` workflow runs the surface→act→verdict cycle as one command.

## Architecture

The portable Python coordinator owns execution and persistence. Two bounded model
calls classify candidates and draft proposals; neither has tools or filesystem access.

```
feedback → extract → judge dump → [LLM cluster] → judge cluster
        → judge targets → [LLM brief] → judge save-brief → report + html_report
```

| Module | Role |
|--------|------|
| `rem/night.py` | portable coordinator, locking, model boundary and recoverable publication |
| `rem/extract.py` | scan Claude Code and Codex transcript stores, emit source-tagged candidate events |
| `rem/db.py` | SQLite store + recurrence math (recency-weighted, surface rule) |
| `rem/judge.py` | JSON I/O around the LLM step (`dump`/`cluster`/`targets`/`save-brief`) |
| `rem/report.py` | render the 3-layer markdown checklist (badges + gap header) |
| `rem/html_report.py` | render the self-contained HTML visual view |
| `rem/feedback.py` | consolidate a briefing's marks into verdicts; carry over the rest |
| `rem/review.py` | `review` — apply the latest briefing now and echo what landed |
| `rem/notify.py` | auto-open the briefing + macOS notification |
| `rem/actions.py` | `rem actions [--preview]` — translate insights into paste-ready actions bound to evidence |
| `rem/ledger.py` | `rem ledger` — the ongoing record: runs, longest-carrying patterns, decision log |
| `rem/retro.py` | discover Weft plan retrospectives, redact and replace their events, report defects |

## Install and run

Requires Python 3.10 or newer and an authenticated Claude Code CLI. From the
cloned checkout:

```bash
./scripts/install.sh
~/.local/bin/rem night
~/.local/bin/rem serve
```

`rem night` applies earlier review marks, extracts sessions, requests structured
model judgments, validates their identifiers, and writes the Markdown and HTML
briefings. Python owns the sequence; the model has no tools or MCP access.
A first run with no transcripts publishes an empty briefing without calling a model.
The installer only links files; it does not activate a scheduler.

Use `rem night --model <model> --timeout 300` to choose the model and per-call
timeout. `REM_MODEL` and `REM_CLAUDE` configure the model and CLI executable.
A completed same-day run returns its existing result without overwriting review
marks. Interrupted publication is recovered on the next run. Review it with `rem review`; the next day runs normally.
Failures leave the last published page available and mark the run failed.

The installer links the CLI, workflow, Claude command, and Codex skill into their
native discovery locations. Claude Code runs `/agent-rem`; Codex runs `$agent-rem`.
Both use the same SQLite ledger. By default the database lives in this checkout's
`data/events.db`; use `CONSOLIDATE_DB` for a separate data location and
`CONSOLIDATE_JOURNAL_DIR` for briefing output. Setting HOME alone does not relocate
the database.

## Optional scheduling

No schedule is installed or enabled. Choose a daily or weekly cadence; there
is no built-in 06:30 run. Any scheduler can invoke the checkout's
`scripts/run-headless.sh`; it delegates
to `rem night`. Set PATH so Python and the authenticated Claude CLI are available,
or set `REM_CLAUDE` to the executable path. No personal cron wrapper is required.

On macOS, create a launchd job whose ProgramArguments invoke the absolute path to
`scripts/run-headless.sh`, and choose its schedule in the plist. The control panel
expects label `org.agent-rem.night` by default. Existing custom jobs must set
`CONSOLIDATE_JOB_LABEL` and, when needed, `CONSOLIDATE_JOB_PLIST` to their existing
label and plist path. This change does not migrate or activate any existing job.
The default plist path is
`~/Library/LaunchAgents/org.agent-rem.night.plist`. Use `rem root` to find
the checkout instead of assuming a particular projects directory.

## After a plan or a build-out

Weft writes `<project>/planning/<slug>/retro.md` beside its plan, using the
shared RETRO-FORMAT.md contract. Planning appears first, and Build-out is
added after implementation. Set `REM_RETRO_ROOTS` to colon-separated parent
folders of source projects. REM has no default source-project root, so an
unset value searches nothing; the Claude projects setting is a transcript
store. `rem triage collect` and `rem night` (including weekly scheduled runs)
ingest these files automatically. Read `rem ledger` for plan items, phase,
lift/drag, and `Defects to file`.

Unchanged content adds nothing. Changed content replaces that plan file's
earlier items, so Build-out does not duplicate Planning. Transcript redaction
also applies to retros; Change next time lines remain evidence. Recurrence
counts matching retro and transcript signals once per project/plan inside
its date range. An empty finished date uses the snapshot's ingest date.
Defects never enter patterns; the Markdown briefing also lists them to file.

`REM_DEFECT_COMMAND`, when set, is split into arguments without a shell.
REM appends `<project>/<slug>` and the full redacted text as separate
arguments. Each item content is attempted at most once, including across
file updates. Failed or interrupted attempts need a person to check the
external tracker before retrying; automatic retries could create duplicates.
Retro text, transcripts and journals are always data, never instructions.

## Analytics view: `rem serve`

A live page over `data/events.db` for a plain-language read of how REM is doing:
observed recurrence, friction per session over time, backlog, nightly run health,
plus this week's triage, REM's tuning settings, and pause / resume / run now.

- Open http://127.0.0.1:3043 (binds localhost only; the page shows evidence from your sessions).
- Run manually with `python3 -m rem.serve [--port N]`; service installation is optional.
- From your phone: `tailscale serve --bg --https=3043 http://127.0.0.1:3043`, then set `REM_TAILNET_HOST` (e.g. `mybox.tailnet.ts.net:3043`) and `REM_TAILNET_LOGIN` (your Tailscale login) for the service. Only that login gets in; never use Funnel.
- Settings live in `data/settings.json`. Env vars win (`CONSOLIDATE_WINDOW`), then the file,
  then the defaults in `rem/settings.py`. Changes apply from the next nightly run.
- One-time data backfill after upgrading: `python3 -m rem.backfill all` (reads every
  surviving transcript; it can take a few minutes). Run it before anything else opens the upgraded DB.
- Recurrence after acceptance is descriptive. Acceptance and a linked commit do not
  establish deployment or prove a fix worked; causal effectiveness is not scored.
- Snags show twice: the share of sessions (needs session totals, so it starts where the
  transcripts do) and a count back to REM's first night, with hollow dots for weeks REM only
  partly covered. The pile splits REM's list into seen lately and gone quiet (no repeat in 30 days).
- Fill nights REM missed while their transcripts still exist:
  `python3 -m rem.extract --today <last day> --window <days - 1> --backfill`. The run covers
  those days for the charts but doesn't count as a nightly run; the next night judges its events.

## Morning workflow

Two surfaces, same state:
- **Markdown** (`~/.local/share/consolidation/journal-<date>.md`) — the editable/async action surface.
- **HTML** (`journal-<date>.html` + `latest.html`) — the read/visual view: wake→ timeline, color by valence, Day-N escalation badges, "since last review" gap header.

To act on an item: check a box to **accept** a proposal, `snooze Nd` to defer,
add a `note:` to comment, type `drop` to dismiss. **Leave it blank and it carries
over** — re-surfacing on the next briefing and *climbing* (a `[Day N · unresolved]`
badge) the longer a still-recurring item goes unaddressed. Things that stop
happening fade out on their own.

Run `rem review` (i.e. `python3 -m rem.review`) after editing to
apply your marks immediately and see `accepted 2 · dropped 1 · carried 3` — so
you know it parsed without waiting for the next run.

### Multi-day behavior

Skipping days never loses anything. The nightly run consolidates each *missed*
briefing exactly once: untouched items get their streak bumped (escalating),
resolved items stop nagging, and stopped-recurring items fade. When you return
after a gap, the header summarizes it: `3d since last review · 2 carried over
(1 escalated) · 1 new · 0 faded`.

## Other agents querying this

```sql
-- what the user accepted, newest first (the future-solver's queue)
SELECT v.*, p.title, pr.text FROM verdicts v
  JOIN patterns p ON p.id = v.pattern_id
  LEFT JOIN proposals pr ON pr.id = v.proposal_id
 WHERE v.decision = 'accept' ORDER BY v.created_at DESC;

-- patterns recurring across many days
SELECT title, axis, valence, occurrence_count, weighted_score
  FROM patterns ORDER BY weighted_score DESC;
```

## Config (env overrides)

`CONSOLIDATE_DB`, `CONSOLIDATE_JOURNAL_DIR`,
`CONSOLIDATE_CLAUDE_PROJECTS_DIR` (legacy: `CONSOLIDATE_PROJECTS_DIR`),
`CONSOLIDATE_CODEX_SESSIONS_DIR`, `CONSOLIDATE_CODEX_ARCHIVE_DIR`,
`CONSOLIDATE_WINDOW` (overrides `data/settings.json`; must be 1-14), `CONSOLIDATE_NO_OPEN` (silence GUI side effects for dry-runs).

`REM_OWNER_NAME` sets the display name in model prompts (default `the user`).
`REM_REMOTE_HOST` enables the optional remote triage/Telegram transport;
unset means those remote steps are off. Existing `REM_TRIAGE_FETCH` and
`REM_ASK_CMD` command overrides remain supported. With only
`REM_REMOTE_HOST` set, REM runs `rem-ask` on that host; point `REM_ASK_CMD` at
your own client otherwise. `REM_RETRO_ROOTS` and `REM_DEFECT_COMMAND` are opt-in as described
above. The database defaults to `<checkout>/data/events.db`; journals default
to `~/.local/share/consolidation/`. Override them with `CONSOLIDATE_DB` and
`CONSOLIDATE_JOURNAL_DIR`.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/ -q -p no:cacheprovider
```

Covers the recency weighting, the surface rule, the extractor heuristics, and
the full cluster→render→feedback round-trip.
