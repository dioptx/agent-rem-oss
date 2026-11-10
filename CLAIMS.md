# README claim evidence

Each row covers a factual README statement. The motivation for preferring a record over instructions is a product tradeoff, not a measured benefit.

| README claim | Backing file and function or test |
| --- | --- |
| REM is a morning review of recurring coding-agent mistakes. | `rem/extract.py:signals_to_events`, `rem/db.py:ConsolidationDB.surface`, `rem/report.py:render` |
| Pitch sequence: a snag becomes a card, a person chooses, later evidence remains visible. | `rem/triage.py:cards`, `rem/static/index.html:renderTriage`, `saveDecisions`, `renderFixes`; `demo/test_seed_demo.py:test_overview_has_nights_friction_decisions_quiet_recurrence_and_untreated` |
| Reads local Claude Code and Codex session histories and groups recurring friction into proposals. | `rem/extract.py:iter_session_files`, `scan_file`; `rem/night.py:run`; `rem/judge.py:cmd_cluster`, `cmd_save_brief` |
| SQLite stores observations, proposals and human decisions. | `rem/db.py:SCHEMA`, `ConsolidationDB.insert_event`, `add_proposal`, `record_verdict` |
| Proposes rather than implements changes; a person accepts, defers, dismisses or silences. | `rem/actions.py:render_action`, `queue`; `rem/feedback.py:parse_journal`, `apply`; `rem/static/index.html:act`, `saveDecisions` |
| Adds recurring observations, decisions and later evidence to an instruction-file workflow; requires keeping a ledger and reviewing proposals. | `rem/db.py:ConsolidationDB`, `rem/review.py:main`, `rem/stats.py:fix_stuck`; the preference between workflows is editorial, not an effectiveness claim |
| Linked evidence does not establish whether a change helped; the demo is not an effectiveness study. | `rem/stats.py:fix_stuck`, `headline`; `demo/seed_demo.py:seed_demo` invents all observations rather than measuring interventions |
| Demo requires Python/SQLite and Bash; the served page is for a browser; tests need pytest. | `rem/db.py` imports `sqlite3`; `demo/run.sh`; `rem/serve.py:Handler._page`; `demo/test_seed_demo.py` imports `pytest` |
| Demo needs no model or transcript archive. | `demo/seed_demo.py:seed_demo` uses fixed constants, `random.Random`, and the database API; `demo/test_seed_demo.py:test_two_seeds_match_every_row_and_generated_file` |
| Live classification uses an authenticated Claude Code CLI; session input supports both agents. | `rem/night.py:call_model`, `main`; `rem/extract.py:iter_session_files`; `rem/config.py` transcript-source constants |
| The demo runs from the repository root; REM_SOURCE can select another checkout. | `demo/run.sh` source selection and source-file check; `demo/terminal-demo.sh` source selection |
| Launcher creates temporary synthetic data and starts the real REM page on loopback. | `demo/run.sh`; `demo/serve_demo.py:main`, `demo_context`; `rem/serve.py:make_server` |
| Ctrl-C removes temporary data; scheduler controls are disabled; demo triage and settings use only demo paths. | `demo/run.sh:cleanup` and traps; `demo/serve_demo.py:demo_runner`, `demo_context`; `rem/serve.py:Handler._save_triage`, `_save_settings`; `demo/test_seed_demo.py:test_real_page_and_api_use_only_the_demo_context` |
| Eight weeks of synthetic nights, changing friction, pending decisions, quieter accepted patterns, a more recurrent pattern, untreated patterns, and one failed final run; no real usage results. | `demo/seed_demo.py:NIGHTS`, `PATTERNS`, `seed_demo`; `demo/test_seed_demo.py:test_overview_has_nights_friction_decisions_quiet_recurrence_and_untreated` |
| Persistent-ledger Quick Start commands exist and read the seeded ledger. | `demo/seed_demo.py:main`; `bin/rem`; `rem/ledger.py:main`; `rem/actions.py:main`; `demo/test_seed_demo.py:test_real_cli_reads_the_seed` |
| Refuses a non-empty destination. | `demo/seed_demo.py:seed_demo`; `demo/test_seed_demo.py:test_refuses_to_open_an_existing_directory` |
| Fixed seed plus date anchor reproduce the ledger and --today pins the date. | `demo/seed_demo.py:SEED`, `seed_demo`, `main`; `demo/test_seed_demo.py:test_two_seeds_match_every_row_and_generated_file` compares every generated file including database bytes |
| Seeder never scans existing databases, journals or transcripts. | `demo/seed_demo.py:seed_demo` checks an empty destination, imports no extractor, and supplies an explicit new database path |
| Terminal script types commands, pauses, runs the actual CLI and removes its temporary data. | `demo/terminal-demo.sh:type_command`, CLI invocations, sleeps and EXIT trap |
| Preview uses the last successful briefing because the final run failed. | `demo/seed_demo.py:seed_demo` final failure and `latest_journal`; `demo/terminal-demo.sh` brief-date lookup; `demo/test_seed_demo.py:test_real_cli_reads_the_seed` |
| Walkthrough can inspect a pending card, choose an idea, and inspect earlier/later evidence. | `rem/static/index.html:cardHTML`, `flip`, `renderFixes`; `demo/test_seed_demo.py:test_real_page_and_api_use_only_the_demo_context` |
| Overview tiles show dated per-agent session counts, waiting decisions and accepted patterns with later evidence. | `rem/stats.py:overview`; `rem/static/index.html:renderTiles` |
| Friction separates agent sessions and marks coverage gaps. | `rem/stats.py:friction_weekly`; `rem/static/index.html:renderFriction`, `GAP_WORD` |
| Cards offer the four named choices, saved only by Save. | `rem/static/index.html:drawDeck`, `act`, `saveDecisions` |
| Accepted proposals keep history and a change reference; acceptance differs from implementation. | `rem/db.py:ConsolidationDB.record_verdict`, `set_fix_pr`; `rem/actions.py:record_implementation`; `tests/test_implementation.py:test_only_accepted_proposal_can_be_reported_and_history_survives` |
| Follow-up shows event groups, scan-window days and earlier evidence. | `rem/stats.py:fix_stuck`; `rem/static/index.html:renderFixes`; `tests/test_followup_page.py:test_followup_render_and_copy_text_are_descriptive` |
| ledger shows accumulated history; actions renders accepted proposals as session-ready text. | `rem/ledger.py:render`; `rem/actions.py:queue_items`, `render_action` |
| Repository layout: CLI, runtime, page, prompts and tests. | `bin/rem`; `rem/db.py:ConsolidationDB`; `rem/extract.py:run`; `rem/night.py:run`; `rem/review.py:main`; `rem/serve.py:main`; `rem/static/index.html`; `rem/night.py:PROMPTS`, `call_model`; `tests/test_night.py:test_model_no_tools_contract` |
| Layout: demo scripts/tests, usage guide and evidence ledger. | `demo/seed_demo.py`, `demo/run.sh`, `demo/terminal-demo.sh`, `demo/test_seed_demo.py`, `docs/usage.md`, this file |
| MIT license. | `LICENSE` |
