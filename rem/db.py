"""SQLite store for consolidation events, patterns, proposals, and verdicts.

Bare sqlite3 + WAL + UPSERT.
Transcript identity and human verdicts persist. A changed plan retrospective
replaces that file's events while retaining classifications of unchanged items.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sessions_scanned INTEGER DEFAULT 0,
    projects TEXT DEFAULT '[]',
    events_extracted INTEGER DEFAULT 0,
    patterns_surfaced INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    occurred_at TEXT,
    occurred_date TEXT NOT NULL,
    session_id TEXT,
    agent TEXT,
    project TEXT,
    git_branch TEXT,
    kind TEXT,
    valence TEXT,
    axis TEXT,
    signature TEXT,
    title TEXT,
    detail TEXT,
    evidence_json TEXT DEFAULT '[]',
    raw_weight INTEGER DEFAULT 1,
    pattern_id INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT,
    axis TEXT,
    valence TEXT,
    title TEXT,
    first_seen TEXT,
    last_seen TEXT,
    occurrence_count INTEGER DEFAULT 0,
    weighted_score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open',
    snoozed_until TEXT,
    surfaced_streak INTEGER DEFAULT 0,
    last_surfaced TEXT,
    last_verdict_at TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id INTEGER NOT NULL,
    tier INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER,
    pattern_id INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    pattern_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    event_line TEXT,
    pattern_line TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS journal_pattern_revisions (
    run_date TEXT NOT NULL,
    pattern_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (run_date, pattern_id)
);

CREATE TABLE IF NOT EXISTS processed_journals (
    run_date TEXT PRIMARY KEY,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS pattern_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    source TEXT
);

CREATE TRIGGER IF NOT EXISTS pattern_created AFTER INSERT ON patterns
BEGIN
  INSERT INTO pattern_history (pattern_id, at, from_status, to_status, source)
  VALUES (NEW.id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL, NEW.status, 'created');
END;

CREATE TRIGGER IF NOT EXISTS pattern_status_changed AFTER UPDATE OF status ON patterns
WHEN OLD.status IS NOT NEW.status
BEGIN
  INSERT INTO pattern_history (pattern_id, at, from_status, to_status, source)
  VALUES (NEW.id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), OLD.status, NEW.status,
          CASE WHEN NEW.status = 'open' THEN 'recurred' ELSE 'verdict' END);
END;

CREATE TABLE IF NOT EXISTS settings_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    key TEXT NOT NULL,
    old TEXT,
    new TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    agent TEXT NOT NULL,
    session_id TEXT NOT NULL,
    started_date TEXT,
    headless INTEGER DEFAULT 0,
    PRIMARY KEY (agent, session_id)
);

CREATE TABLE IF NOT EXISTS event_archive (
    event_id TEXT PRIMARY KEY,
    canonical_id TEXT REFERENCES events(id),
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    row_json TEXT NOT NULL,
    archived_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS prevent_archived_event_reimport BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM event_archive WHERE event_id=NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'Archived event: use the current REM importer');
END;

CREATE TABLE IF NOT EXISTS implementations (
    proposal_id INTEGER PRIMARY KEY REFERENCES proposals(id),
    effective_on TEXT NOT NULL,
    scope TEXT NOT NULL,
    reference TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retro_files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    ingested_on TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS retro_defect_calls (
    event_id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run_date ON events(run_date);
CREATE INDEX IF NOT EXISTS idx_briefs_run_date ON briefs(run_date);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);
CREATE INDEX IF NOT EXISTS idx_events_pattern ON events(pattern_id);
CREATE INDEX IF NOT EXISTS idx_patterns_signature ON patterns(signature);
CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns(status);
CREATE INDEX IF NOT EXISTS idx_verdicts_run_date ON verdicts(run_date);
CREATE INDEX IF NOT EXISTS idx_history_pattern ON pattern_history(pattern_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_between(earlier: str, later: str) -> int:
    """Whole days from `earlier` to `later`, both YYYY-MM-DD."""
    d0 = date.fromisoformat(earlier)
    d1 = date.fromisoformat(later)
    return (d1 - d0).days


class ConsolidationDB:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self._transaction_depth = 0
        self.conn.row_factory = sqlite3.Row
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.conn.close()
            raise RuntimeError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        with self.transaction():
            self._ensure_columns()
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _commit(self) -> None:
        if not self._transaction_depth:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        """Keep a command atomic while retaining standalone helper commits."""
        depth = self._transaction_depth
        savepoint = f"rem_command_{depth}"
        self.conn.execute(f"SAVEPOINT {savepoint}" if depth else "BEGIN IMMEDIATE")
        self._transaction_depth += 1
        try:
            yield
            if depth:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.conn.commit()
        except BaseException:
            if depth:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.conn.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def _ensure_columns(self) -> None:
        """Add columns introduced after a DB was first created (escalation)."""
        if 'metadata_json' not in {row['name'] for row in self.conn.execute('PRAGMA table_info(retro_files)')}:
            self.conn.execute("ALTER TABLE retro_files ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(patterns)")}
        for col, ddl in (("surfaced_streak", "INTEGER DEFAULT 0"),
                         ("last_surfaced", "TEXT"), ("last_verdict_at", "TEXT"),
                         ("prior_decision", "TEXT"), ("fix_pr", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE patterns ADD COLUMN {col} {ddl}")
        event_have = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(events)")
        }
        if "signal_count" not in event_have:
            self.conn.execute("ALTER TABLE events ADD COLUMN signal_count INTEGER")
        if "agent" not in event_have:
            self.conn.execute("ALTER TABLE events ADD COLUMN agent TEXT")
        for col, ddl in (("source", "TEXT DEFAULT 'transcript'"), ("plan_slug", "TEXT"),
                         ("phase", "TEXT"), ("plan_started", "TEXT"),
                         ("plan_finished", "TEXT"), ("retro_path", "TEXT")):
            if col not in event_have:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
        brief_have = {r["name"] for r in self.conn.execute("PRAGMA table_info(briefs)")}
        for col, ddl in (("recommend_tier", "INTEGER"), ("recommend_why", "TEXT"),
                         ("evidence_json", "TEXT")):
            if col not in brief_have:
                self.conn.execute(f"ALTER TABLE briefs ADD COLUMN {col} {ddl}")
        proposal_have = {r["name"] for r in self.conn.execute("PRAGMA table_info(proposals)")}
        for col, ddl in (("run_date", "TEXT"), ("active", "INTEGER NOT NULL DEFAULT 1")):
            if col not in proposal_have:
                self.conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} {ddl}")
        run_have = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "window_days" not in run_have:
            self.conn.execute("ALTER TABLE runs ADD COLUMN window_days INTEGER")
        if "error" not in run_have:
            self.conn.execute("ALTER TABLE runs ADD COLUMN error TEXT")
        if "provenance_json" not in run_have:
            self.conn.execute("ALTER TABLE runs ADD COLUMN provenance_json TEXT DEFAULT '{}'")

    # ---- runs -------------------------------------------------------------
    def close_stale_runs(self, max_minutes: int = 60) -> int:
        """Close runs still 'running' after max_minutes as failed. A run killed from outside
        never reaches its own finally, so the next start does it."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_minutes)).isoformat()
        cur = self.conn.execute(
            "UPDATE runs SET status='failed', finished_at=?, error=? "
            "WHERE finished_at IS NULL AND status='running' AND started_at < ?",
            (_now(), f"abandoned: still running after {max_minutes} min", cutoff))
        self._commit()
        return cur.rowcount

    def start_run(self, run_date: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (run_date, started_at) VALUES (?, ?)",
            (run_date, _now()),
        )
        self._commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, **fields) -> None:
        fields.setdefault("finished_at", _now())
        fields.setdefault("status", "ok")
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [run_id]
        self.conn.execute(f"UPDATE runs SET {cols} WHERE id=?", vals)
        self._commit()

    def upsert_session(self, agent: str, session_id: str, started_date: str | None,
                       headless: bool) -> None:
        """One row per transcript: the friction-rate denominator. Keeps the earliest start."""
        self.conn.execute(
            """INSERT INTO sessions (agent, session_id, started_date, headless)
               VALUES (?,?,?,?)
               ON CONFLICT(agent, session_id) DO UPDATE SET
                 started_date=COALESCE(MIN(sessions.started_date, excluded.started_date),
                                       sessions.started_date, excluded.started_date),
                 headless=MAX(sessions.headless, excluded.headless)""",
            (agent, session_id, started_date, int(bool(headless))))
        self._commit()

    # ---- events -----------------------------------------------------------
    def insert_event(self, ev: dict) -> None:
        """Refresh growing source buckets without replacing their identity or decisions.

        Only extractor records carrying signal_count refresh existing evidence. A changed
        bucket is offered for judgment again; unchanged scans preserve its classification.
        """
        with self.transaction():
            archived = self.conn.execute(
                "SELECT canonical_id FROM event_archive WHERE event_id=?", (ev["id"],)
            ).fetchone()
            if archived is not None:
                if archived["canonical_id"] is None:
                    return
                ev = {**ev, "id": archived["canonical_id"]}
            previous = self.conn.execute("SELECT * FROM events WHERE id=?", (ev["id"],)).fetchone()
            evidence = json.dumps(ev.get("evidence", []))
            if previous is not None:
                if "signal_count" in ev and (
                        previous["signal_count"] != ev["signal_count"]
                        or previous["detail"] != ev.get("detail")
                        or previous["evidence_json"] != evidence):
                    self.conn.execute(
                        "UPDATE events SET detail=?, evidence_json=?, signal_count=?, "
                        "signature=NULL WHERE id=?",
                        (ev.get("detail"), evidence, ev["signal_count"], ev["id"]))
                    self._commit()
                return
            self.conn.execute(
                """INSERT INTO events
                   (id, run_date, occurred_at, occurred_date, session_id, agent, project,
                    git_branch, kind, valence, axis, signature, title, detail,
                    evidence_json, raw_weight, pattern_id, created_at, signal_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ev["id"], ev["run_date"], ev.get("occurred_at"), ev["occurred_date"],
                 ev.get("session_id"), ev.get("agent"), ev.get("project"), ev.get("git_branch"),
                 ev.get("kind"), ev.get("valence"), ev.get("axis"), ev.get("signature"),
                 ev.get("title"), ev.get("detail"), evidence, ev.get("raw_weight", 1),
                 ev.get("pattern_id"), _now(), ev.get("signal_count")))
            self.conn.execute(
                'UPDATE events SET source=?, plan_slug=?, phase=?, plan_started=?, plan_finished=?, retro_path=? WHERE id=?',
                (ev.get('source', 'transcript'), ev.get('plan_slug'), ev.get('phase'), ev.get('plan_started'),
                 ev.get('plan_finished'), ev.get('retro_path'), ev['id']))
            self._commit()

    def candidates(self) -> list[dict]:
        """Include incomplete assignments left by older, non-atomic clustering runs."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE COALESCE(kind, '') != 'defect' "
            "AND (signature IS NULL OR pattern_id IS NULL) ORDER BY occurred_date"
        ).fetchall()
        return [dict(r) for r in rows]

    def tag_event(self, event_id: str, axis: str, valence: str, signature: str) -> None:
        self.conn.execute(
            "UPDATE events SET axis=?, valence=CASE WHEN source='retro' THEN valence ELSE ? END, signature=? WHERE id=? AND COALESCE(kind, '') != 'defect'",
            (axis, valence, signature, event_id),
        )
        self._commit()

    def assign_event(self, event_id: str, pattern_id: int) -> None:
        self.conn.execute(
            "UPDATE events SET pattern_id=? WHERE id=? AND COALESCE(kind, '') != 'defect'", (pattern_id, event_id)
        )
        self._commit()

    # ---- patterns ---------------------------------------------------------
    def open_patterns(self, today: str) -> list[dict]:
        """Patterns eligible to receive new events: open, or snooze expired."""
        rows = self.conn.execute(
            """SELECT * FROM patterns
               WHERE status='open'
                  OR (status='snoozed' AND (snoozed_until IS NULL OR snoozed_until<=?))""",
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]

    def decided_patterns(self, since: str) -> list[dict]:
        """Accepted, dropped or muted patterns decided on or after `since`, offered to the
        nightly judge so a recurrence reattaches instead of minting a new pattern."""
        rows = self.conn.execute(
            "SELECT * FROM patterns WHERE status IN ('accepted','dropped','muted') "
            "AND last_verdict_at >= ? ORDER BY id", (since,)).fetchall()
        return [dict(r) for r in rows]

    def create_pattern(self, signature: str, axis: str, valence: str,
                       title: str, seen: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO patterns
               (signature, axis, valence, title, first_seen, last_seen,
                status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,'open',?,?)""",
            (signature, axis, valence, title, seen, seen, _now(), _now()),
        )
        self._commit()
        return cur.lastrowid

    def reactivate_pattern(self, pattern_id: int, last_seen: str) -> None:
        """A snoozed pattern recurred past its snooze window: reopen it."""
        self.conn.execute(
            """UPDATE patterns SET status='open', snoozed_until=NULL,
               last_seen=?, updated_at=? WHERE id=? AND status='snoozed'""",
            (last_seen, _now(), pattern_id),
        )
        self._commit()

    def find_pattern_by_signature(self, signature: str) -> dict | None:
        """Most recent pattern with this signature, whatever its status.

        The nightly LLM only sees open and recently decided patterns, so a muted/accepted/dropped
        signature recurring would otherwise spawn a fresh duplicate. Looking it
        up by signature lets clustering reattach to the original — that's what
        makes `mute` stick and lets an accepted fix be measured against its own
        history when it recurs."""
        row = self.conn.execute(
            "SELECT * FROM patterns WHERE signature=? ORDER BY id DESC LIMIT 1",
            (signature,),
        ).fetchone()
        return dict(row) if row else None

    def reopen_with_context(self, pattern_id: int, last_seen: str,
                            prior: str) -> None:
        """A previously accepted/dropped signature recurred: reopen it fresh
        (streak reset to Day 1) but remember the prior verdict so the briefing
        can flag 'you already acted on this and it came back'."""
        self.conn.execute(
            """UPDATE patterns SET status='open', prior_decision=?,
               snoozed_until=NULL, surfaced_streak=0, last_seen=?, updated_at=?
               WHERE id=?""",
            (prior, last_seen, _now(), pattern_id),
        )
        self._commit()

    def set_pattern_status(self, pattern_id: int, status: str,
                           snoozed_until: str | None = None) -> None:
        self.conn.execute(
            "UPDATE patterns SET status=?, snoozed_until=?, updated_at=? WHERE id=?",
            (status, snoozed_until, _now(), pattern_id),
        )
        self._commit()

    def recompute_stats(self, today: str, window: int = config.DEFAULT_WINDOW) -> None:
        """Recompute occurrence_count + weighted_score for every pattern from its
        events inside the trailing window.

        occurrence_count is the raw in-window count (the recurrence bar stays
        honest). weighted_score folds two dimensions: recency (yesterday ×3) and
        per-kind severity (a real tool error weighs more than a keyword
        correction) — so the score reflects what's worth acting on, not just how
        often a noisy proxy fired."""
        pats = self.conn.execute("SELECT id FROM patterns").fetchall()
        for p in pats:
            evs = self.conn.execute(
                "SELECT * FROM events WHERE pattern_id=? AND signature IS NOT NULL AND COALESCE(kind, '') != 'defect'", (p["id"],)
            ).fetchall()
            plans = {(e['project'], e['plan_slug']): (e['plan_started'], e['plan_finished'])
                     for e in evs if e['source'] == 'retro' and e['plan_slug'] and e['plan_started'] and e['plan_finished']}
            weights = {}
            last_seen = None
            for e in evs:
                days_ago = _days_between(e["occurred_date"], today)
                w = config.recency_weight(days_ago, window)
                if w > 0:
                    key = ('event', e['id'])
                    if e['source'] == 'retro' and e['plan_slug']:
                        key = ('plan', e['project'], e['plan_slug'])
                    elif e['source'] in (None, 'transcript'):
                        for (project, slug), (started, finished) in sorted(plans.items()):
                            if e['project'] == project and started <= e['occurred_date'] <= finished:
                                key = ('plan', project, slug)
                                break
                    weights[key] = max(weights.get(key, 0), w * config.kind_severity(e['kind']))
                if last_seen is None or e["occurred_date"] > last_seen:
                    last_seen = e["occurred_date"]
            self.conn.execute(
                """UPDATE patterns SET occurrence_count=?, weighted_score=?,
                   last_seen=COALESCE(?, last_seen), updated_at=? WHERE id=?""",
                (len(weights), round(sum(weights.values())), last_seen, _now(), p["id"]),
            )
        self._commit()

    def surface(self, limit: int = config.SURFACE_LIMIT,
                min_occ: int = config.SURFACE_MIN_OCCURRENCES) -> list[dict]:
        """Apply the surface rule against current (recomputed) pattern stats.

        Patterns clearing the recurrence bar (>= min_occ in window) rank by
        weighted_score. If fewer than 3 clear it, backfill by score so the
        briefing is never empty — but never include zero-weight patterns, and
        never exceed `limit`. Both valences are eligible; if no lift made the
        cut but a positive one exists, swap in the strongest lift so wins keep
        getting reinforced.
        """
        rows = self.conn.execute(
            "SELECT * FROM patterns WHERE status='open' AND weighted_score > 0"
        ).fetchall()
        ranked = []
        for r in rows:
            p = dict(r)
            streak = p.get("surfaced_streak") or 0
            esc = 1 + min(streak, config.ESCALATION_CAP) * config.ESCALATION_FACTOR
            p["rank"] = p["weighted_score"] * esc
            ranked.append(p)
        # escalated rank first; ties by raw recurrence, then stable by id
        ranked.sort(key=lambda p: (-p["rank"], -p["occurrence_count"], p["id"]))

        primary = [p for p in ranked if p["occurrence_count"] >= min_occ]
        if len(primary) >= 3:
            chosen = primary[:limit]
        else:
            # backfill from the full ranked list, preserving order, up to limit
            chosen = ranked[: max(3, len(primary))][:limit]

        if chosen and not any(p["valence"] == "lift" for p in chosen):
            top_lift = next((p for p in ranked if p["valence"] == "lift"), None)
            if top_lift and top_lift not in chosen:
                chosen[-1] = top_lift
        return chosen

    # ---- proposals --------------------------------------------------------
    def clear_proposals(self, pattern_id: int, run_date: str | None = None) -> None:
        """Retire a proposal set without breaking old journal or verdict references."""
        if run_date is None:
            self.conn.execute("UPDATE proposals SET active=0 WHERE pattern_id=?", (pattern_id,))
        else:
            self.conn.execute(
                "UPDATE proposals SET active=0 WHERE pattern_id=? AND (run_date=? OR run_date IS NULL)",
                (pattern_id, run_date))
        self._commit()

    def add_proposal(self, pattern_id: int, tier: int, text: str,
                     run_date: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO proposals (pattern_id, tier, text, created_at, run_date) VALUES (?,?,?,?,?)",
            (pattern_id, tier, text, _now(), run_date),
        )
        self._commit()
        return cur.lastrowid

    def proposals_for(self, pattern_id: int, run_date: str | None = None,
                      include_archived: bool = False) -> list[dict]:
        if include_archived:
            rows = self.conn.execute(
                "SELECT * FROM proposals WHERE pattern_id=? ORDER BY tier, id", (pattern_id,))
        else:
            if run_date is None:
                row = self.conn.execute(
                    "SELECT MAX(run_date) FROM proposals WHERE pattern_id=? AND active=1",
                    (pattern_id,)).fetchone()
                run_date = row[0]
            rows = self.conn.execute(
                "SELECT * FROM proposals WHERE pattern_id=? AND active=1 "
                "AND (run_date=? OR run_date IS NULL) ORDER BY tier, id", (pattern_id, run_date))
        return [dict(r) for r in rows]

    # ---- verdicts ---------------------------------------------------------
    def record_verdict(self, pattern_id: int, run_date: str, decision: str,
                       proposal_id: int | None = None, comment: str = "",
                       created_at: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO verdicts
               (proposal_id, pattern_id, run_date, decision, comment, created_at)
               VALUES (?,?,?,?,?,?)""",
            (proposal_id, pattern_id, run_date, decision, comment, created_at or _now()),
        )
        self._commit()

    def verdict_times(self, run_date: str) -> dict:
        """First-applied time per verdict, so re-running review keeps the decision date."""
        rows = self.conn.execute(
            "SELECT pattern_id, proposal_id, decision, MIN(created_at) t FROM verdicts "
            "WHERE run_date=? GROUP BY pattern_id, proposal_id, decision", (run_date,)).fetchall()
        return {(r["pattern_id"], r["proposal_id"], r["decision"]): r["t"] for r in rows}

    def verdicts_exist_for(self, run_date: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM verdicts WHERE run_date=? LIMIT 1", (run_date,)
        ).fetchone() is not None

    def clear_verdicts(self, run_date: str) -> None:
        """Drop prior verdicts for a journal so `review` can be re-run after edits."""
        self.conn.execute("DELETE FROM verdicts WHERE run_date=?", (run_date,))
        self._commit()

    def accepted_verdicts(self, since: str | None = None) -> list[dict]:
        """Accepted proposals, newest first — the queue an action list works from."""
        if since:
            rows = self.conn.execute(
                "SELECT * FROM verdicts WHERE decision='accept' AND run_date>=? "
                "ORDER BY created_at DESC", (since,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM verdicts WHERE decision='accept' "
                "ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def last_review_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(run_date) d FROM verdicts").fetchone()
        return row["d"] if row and row["d"] else None

    def journal_processed(self, run_date: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM processed_journals WHERE run_date=?", (run_date,)
        ).fetchone() is not None

    def mark_journal_processed(self, run_date: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_journals (run_date, processed_at) "
            "VALUES (?, ?)", (run_date, _now()))
        self._commit()

    # ---- escalation bookkeeping ------------------------------------------
    def bump_streak(self, pattern_id: int) -> None:
        """A surfaced pattern got no verdict: it carries over and climbs."""
        self.conn.execute(
            "UPDATE patterns SET surfaced_streak=COALESCE(surfaced_streak,0)+1, "
            "updated_at=? WHERE id=?", (_now(), pattern_id))
        self._commit()

    def mark_resolved(self, pattern_id: int, when: str) -> None:
        """A verdict landed: reset the streak and stamp the review time."""
        self.conn.execute(
            "UPDATE patterns SET surfaced_streak=0, last_verdict_at=?, updated_at=? "
            "WHERE id=?", (when, _now(), pattern_id))
        self._commit()

    def set_patterns_surfaced(self, run_date: str, n: int) -> None:
        """Record how many patterns a run surfaced, so the ledger's run timeline
        reflects reality instead of the 0 default."""
        self.conn.execute(
            "UPDATE runs SET patterns_surfaced=? WHERE run_date=?", (n, run_date))
        self._commit()

    def mark_surfaced(self, pattern_ids: list[int], run_date: str) -> None:
        for pid in pattern_ids:
            self.conn.execute(
                "UPDATE patterns SET last_surfaced=?, updated_at=? WHERE id=?",
                (run_date, _now(), pid))
        self._commit()

    def faded_count(self) -> int:
        """Patterns that were recurring (streak>0) but have stopped happening."""
        return self.conn.execute(
            "SELECT COUNT(*) c FROM patterns WHERE status='open' "
            "AND COALESCE(surfaced_streak,0) > 0 AND weighted_score = 0"
        ).fetchone()["c"]

    def set_fix_pr(self, pattern_id: int, url: str) -> None:
        """The PR the act step opened for this pattern, so a recurrence can say whether it merged."""
        self.conn.execute("UPDATE patterns SET fix_pr=?, updated_at=? WHERE id=?", (url, _now(), pattern_id))
        self._commit()

    def log_setting(self, key: str, old, new, source: str = "ui") -> None:
        """One row per setting change or job action (pause/resume/run now)."""
        self.conn.execute(
            "INSERT INTO settings_log (at, key, old, new, source) VALUES (?,?,?,?,?)",
            (_now(), key, None if old is None else str(old),
             None if new is None else str(new), source))
        self._commit()

    def get_pattern(self, pattern_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM patterns WHERE id=?", (pattern_id,)
        ).fetchone()
        return dict(row) if row else None

    def events_for_pattern(self, pattern_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE pattern_id=? ORDER BY occurred_date DESC",
            (pattern_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- briefs (per-run Event/Pattern framing written by the LLM) --------
    def clear_briefs(self, run_date: str) -> None:
        self.conn.execute("DELETE FROM briefs WHERE run_date=?", (run_date,))
        self._commit()

    def add_brief(self, run_date: str, pattern_id: int, position: int,
                  event_line: str, pattern_line: str,
                  recommend_tier: int | None = None, recommend_why: str = "") -> None:
        self.conn.execute(
            """INSERT INTO briefs
               (run_date, pattern_id, position, event_line, pattern_line, recommend_tier,
                recommend_why, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_date, pattern_id, position, event_line, pattern_line, recommend_tier,
             recommend_why, _now()),
        )
        self._commit()

    def briefs_for(self, run_date: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM briefs WHERE run_date=? ORDER BY position ASC",
            (run_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()
