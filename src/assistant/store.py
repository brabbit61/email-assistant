"""SQLite store: schema, migrations, connection (T1.2, issue #8).

The database is **append-only** — every write is an INSERT; no row is ever
UPDATEd or DELETEd. Lifecycle state (an action's intended→confirmed/failed, a
run's started→finished) is recorded as immutable event rows in `action_events`
and `run_events`. "Current state" is *derived* (latest event per id) via the
`current_actions` and `current_checkpoint` views.

Schema is versioned with `PRAGMA user_version`; `open_db()` applies any pending
migrations from `_MIGRATIONS` on open. No ORM.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# One entry per schema version. Index i defines the migration from version i to
# i+1. Append new entries here; never edit a shipped one.
_MIGRATIONS: list[str] = [
    # v0 -> v1: initial append-only schema
    """
    CREATE TABLE messages (
        gmail_message_id TEXT PRIMARY KEY,
        thread_id        TEXT,
        sender           TEXT,
        subject          TEXT,
        snippet          TEXT,
        body             TEXT,
        internal_date_ms INTEGER,
        first_seen_at    TEXT NOT NULL
    );

    CREATE TABLE llm_calls (
        id               INTEGER PRIMARY KEY,
        actor            TEXT NOT NULL,
        purpose          TEXT NOT NULL,
        model            TEXT NOT NULL,
        input_tokens     INTEGER NOT NULL,
        output_tokens    INTEGER NOT NULL,
        cost_usd         REAL NOT NULL,
        gmail_message_id TEXT REFERENCES messages(gmail_message_id),
        created_at       TEXT NOT NULL
    );

    CREATE TABLE classifications (
        id               INTEGER PRIMARY KEY,
        gmail_message_id TEXT NOT NULL REFERENCES messages(gmail_message_id),
        category         TEXT NOT NULL,
        priority         TEXT,
        reasoning        TEXT,
        llm_call_id      INTEGER REFERENCES llm_calls(id),
        classified_at    TEXT NOT NULL
    );

    CREATE TABLE action_events (
        event_id         INTEGER PRIMARY KEY,
        action_id        TEXT NOT NULL,
        status           TEXT NOT NULL,
        action_type      TEXT NOT NULL,
        actor            TEXT NOT NULL,
        run_id           TEXT,
        gmail_message_id TEXT REFERENCES messages(gmail_message_id),
        thread_id        TEXT,
        detail           TEXT,
        error            TEXT,
        recorded_at      TEXT NOT NULL
    );

    CREATE TABLE run_events (
        event_id      INTEGER PRIMARY KEY,
        run_id        TEXT NOT NULL,
        phase         TEXT NOT NULL,
        status        TEXT,
        messages_seen INTEGER,
        actions_taken INTEGER,
        error_count   INTEGER,
        history_id    TEXT,
        note          TEXT,
        recorded_at   TEXT NOT NULL
    );

    CREATE INDEX idx_action_events_action_id ON action_events(action_id);
    CREATE INDEX idx_action_events_status    ON action_events(status);
    CREATE INDEX idx_action_events_message   ON action_events(gmail_message_id);
    CREATE INDEX idx_run_events_run_id       ON run_events(run_id);
    CREATE INDEX idx_classifications_message ON classifications(gmail_message_id);
    CREATE INDEX idx_llm_calls_created       ON llm_calls(created_at);

    -- Current verdict/state derived from the append-only logs.
    CREATE VIEW current_actions AS
        SELECT ae.* FROM action_events ae
        WHERE ae.event_id = (
            SELECT MAX(e2.event_id) FROM action_events e2
            WHERE e2.action_id = ae.action_id
        );

    CREATE VIEW current_checkpoint AS
        SELECT run_id, history_id, recorded_at
        FROM run_events
        WHERE phase = 'finished' AND status = 'ok'
        ORDER BY event_id DESC
        LIMIT 1;
    """,
]


def now_iso() -> str:
    """Current UTC time as ISO-8601 text (the schema's timestamp format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    """A logical id (action_id / run_id) shared by an entity's event rows."""
    return uuid.uuid4().hex


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the project's pragmas set. Creates the data dir."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "PRAGMA journal_mode=WAL"
    )  # readers (agent) don't block the writer (worker)
    conn.execute(
        "PRAGMA busy_timeout=5000"
    )  # wait, don't instant-fail, on a write collision
    conn.execute(
        "PRAGMA foreign_keys=ON"
    )  # off by default in SQLite; needed for the FKs
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(_MIGRATIONS)):
        conn.executescript(_MIGRATIONS[i])
        conn.execute(f"PRAGMA user_version = {i + 1}")  # int, not user input
    conn.commit()


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Connect and bring the schema up to SCHEMA_VERSION. Idempotent."""
    conn = connect(db_path)
    _migrate(conn)
    return conn
