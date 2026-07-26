"""SQLite store: schema and connection.

The database is **append-only** — every write is an INSERT; no row is ever
UPDATEd or DELETEd. Lifecycle state (an action's intended→confirmed/failed, a
run's started→finished) is recorded as immutable event rows in `action_events`
and `run_events`. "Current state" is *derived* (latest event per id) via the
`current_actions` and `current_checkpoint` views.

The full schema is created in one shot on first open (`_SCHEMA`). `open_db()` is
idempotent: it stamps a new database with `SCHEMA_VERSION` and leaves an
already-current one untouched. No migration ladder, no ORM.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The complete schema, created in full on the first open of a fresh database.
_SCHEMA = """
CREATE TABLE messages (
    gmail_message_id TEXT PRIMARY KEY,
    thread_id        TEXT,
    sender           TEXT,
    subject          TEXT,
    body             TEXT,
    internal_date_ms INTEGER,
    gmail_label_ids  TEXT,
    raw_json         TEXT,
    first_seen_at    TEXT NOT NULL,
    -- 'poll' = live inbox mail; 'backfill' = historical import. The live loop
    -- excludes origin != 'poll' so backfilled mail never re-enters triage.
    origin           TEXT NOT NULL DEFAULT 'poll' CHECK(origin IN ('poll','backfill')),
    -- Stable INTEGER rowid snapshot for the external-content FTS index below
    -- (messages' own PK is TEXT, and the implicit rowid can be renumbered by VACUUM).
    fts_rowid        INTEGER
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
    created_at       TEXT NOT NULL,
    -- Natural key for hermes-imported rows (NULL for worker rows); the partial
    -- unique index below makes `import-hermes` re-runnable without double-counting.
    hermes_session_id TEXT
);

-- category/priority CHECKs are the persistence guarantee: an off-taxonomy value
-- raises on INSERT and the classifier rewrites the row as UNCLASSIFIED.
-- ponytail: category list is duplicated from labels.CATEGORIES (a schema string
-- can't import) + 'UNCLASSIFIED'; test_classify asserts they match.
-- `source`: 'worker' = the classifier; 'human-chat' = `assistant correct`;
-- 'human-gmail' = a relabel detected on poll; 'backfill' = historical import.
CREATE TABLE classifications (
    id               INTEGER PRIMARY KEY,
    gmail_message_id TEXT NOT NULL REFERENCES messages(gmail_message_id),
    category         TEXT NOT NULL CHECK(category IN (
                         'Action-Needed','Finance','Bills','Orders','Events',
                         'Travel','Work','Personal','Dev','Newsletters',
                         'Low-Value','UNCLASSIFIED')),
    priority         TEXT CHECK(priority IN
                         ('P1-Urgent','P2-This-Week','P3-FYI') OR priority IS NULL),
    reasoning        TEXT,
    llm_call_id      INTEGER REFERENCES llm_calls(id),
    classified_at    TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'worker'
                         CHECK(source IN ('worker','human-chat','human-gmail','backfill'))
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
    recorded_at   TEXT NOT NULL,
    -- In-flight Batch API id the resumable `backfill --run` re-polls instead of
    -- resubmitting (no double-spend); mirrors how history_id types the checkpoint.
    batch_id      TEXT
);

CREATE INDEX idx_action_events_action_id ON action_events(action_id);
CREATE INDEX idx_action_events_status    ON action_events(status);
CREATE INDEX idx_action_events_message   ON action_events(gmail_message_id);
CREATE INDEX idx_run_events_run_id       ON run_events(run_id);
CREATE INDEX idx_classifications_message ON classifications(gmail_message_id);
CREATE INDEX idx_llm_calls_created       ON llm_calls(created_at);
CREATE UNIQUE INDEX idx_llm_calls_hermes_session ON llm_calls(hermes_session_id)
    WHERE hermes_session_id IS NOT NULL;
CREATE UNIQUE INDEX idx_messages_fts_rowid ON messages(fts_rowid);

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

CREATE VIEW current_classifications AS
    SELECT c.* FROM classifications c
    WHERE c.id = (
        SELECT MAX(c2.id) FROM classifications c2
        WHERE c2.gmail_message_id = c.gmail_message_id
    );

-- FTS5 search index over messages. External-content mode (content='messages')
-- avoids duplicating text; content_rowid points at the stable fts_rowid column.
CREATE VIRTUAL TABLE messages_fts USING fts5(
    sender, subject, body,
    content='messages', content_rowid='fts_rowid',
    tokenize='porter unicode61'
);

-- New rows only: SQLite can't mutate the row being inserted from a BEFORE
-- trigger, so this is AFTER, where NEW.rowid is already assigned. WHEN
-- NEW.fts_rowid IS NULL means app inserts (which never set the column) fire it.
CREATE TRIGGER trg_messages_fts_insert AFTER INSERT ON messages
WHEN NEW.fts_rowid IS NULL
BEGIN
    UPDATE messages SET fts_rowid = NEW.rowid
        WHERE gmail_message_id = NEW.gmail_message_id;
    INSERT INTO messages_fts(rowid, sender, subject, body)
        VALUES (NEW.rowid, NEW.sender, NEW.subject, NEW.body);
END;
"""

# The version this schema represents. Existing databases already stamped with
# this value are recognized as current and left untouched. Bump it only when you
# change `_SCHEMA`, and add explicit handling for the older value in `_init_schema`.
SCHEMA_VERSION = 7


def now_iso() -> str:
    """Current UTC time as ISO-8601 text (the schema's timestamp format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    """A logical id (action_id / run_id) shared by an entity's event rows."""
    return uuid.uuid4().hex


def clock(recorded_at: str) -> str:
    """HH:MM (UTC) from a schema timestamp — for ping/status human output."""
    return recorded_at[11:16]


def age(recorded_at: str) -> str:
    """Human 'time since' for a schema timestamp: 'just now' / 'Nm ago' / 'Nh
    ago' / 'Nd ago'. Shared by `assistant status` and the worker's alert pings."""
    then = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


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


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema on a fresh database; no-op on an already-current one.

    A database at `SCHEMA_VERSION` is left exactly as-is. A brand-new one (version
    0) gets the full `_SCHEMA` and is stamped. Any other version is a database
    this build can't safely use — this project ships a single consolidated schema
    and carries no incremental migrations."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version == 0:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")  # int, not user input
        conn.commit()
        return
    raise RuntimeError(
        f"Database at {conn.execute('PRAGMA database_list').fetchone()[2]!r} is "
        f"schema version {version}, but this build expects {SCHEMA_VERSION}. "
        "This project ships a single consolidated schema and no longer carries "
        "the incremental migrations to convert other versions. Start from a fresh "
        "database (the worker rebuilds state on its next poll)."
    )


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Connect and ensure the schema exists at SCHEMA_VERSION. Idempotent."""
    conn = connect(db_path)
    _init_schema(conn)
    return conn
