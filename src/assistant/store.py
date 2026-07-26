"""SQLite store: schema, migrations, connection.

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
        body             TEXT,
        internal_date_ms INTEGER,
        gmail_label_ids TEXT,
        raw_json TEXT,
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

    -- category/priority CHECKs are the persistence guarantee: an off-taxonomy
    -- value raises on INSERT and the classifier rewrites the row as UNCLASSIFIED.
    -- ponytail: category list is duplicated from labels.CATEGORIES (a migration
    -- string can't import) + 'UNCLASSIFIED'; test_classify asserts they match.
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
    """
    CREATE VIEW current_classifications AS
        SELECT c.* FROM classifications c
        WHERE c.id = (
            SELECT MAX(c2.id) FROM classifications c2
            WHERE c2.gmail_message_id = c.gmail_message_id
        );
    """,
    """
    -- Natural key for hermes-imported rows (NULL for worker rows); a partial
    -- unique index makes `import-hermes` re-runnable without double-counting.
    ALTER TABLE llm_calls ADD COLUMN hermes_session_id TEXT;
    CREATE UNIQUE INDEX idx_llm_calls_hermes_session ON llm_calls(hermes_session_id)
        WHERE hermes_session_id IS NOT NULL;
    """,
    """
    -- v3 -> v4: who originated a classification (improvement loop).
    -- 'worker' = the classifier; 'human-chat' = `assistant correct`; 'human-gmail'
    -- = a relabel detected on poll. The review reads rows where source != 'worker'.
    -- `SELECT c.*` in current_classifications carries the column through unchanged.
    ALTER TABLE classifications ADD COLUMN source TEXT NOT NULL DEFAULT 'worker'
        CHECK(source IN ('worker','human-chat','human-gmail'));
    """,
    """
    -- v4 -> v5: historical backfill support. Two changes:
    --  (a) run_events gets a typed batch_id column — the in-flight Batch API id
    --      the resumable `backfill --run` re-polls instead of resubmitting (no
    --      double-spend). Mirrors how history_id already types the triage
    --      checkpoint in this same table.
    ALTER TABLE run_events ADD COLUMN batch_id TEXT;

    --  (b) classifications.source must accept 'backfill'. SQLite can't ALTER a
    --      CHECK, so rebuild the table (nothing FK-references classifications, so
    --      a straight copy of every append-only row is safe). Column list/order
    --      matches the post-v4 table exactly. The current_classifications view
    --      references the table by name, so drop it first and recreate it after
    --      the rename (its definition is unchanged from v1->v2).
    DROP VIEW current_classifications;
    CREATE TABLE classifications_new (
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
    INSERT INTO classifications_new
        SELECT id, gmail_message_id, category, priority, reasoning, llm_call_id,
               classified_at, source FROM classifications;
    DROP TABLE classifications;
    ALTER TABLE classifications_new RENAME TO classifications;
    CREATE INDEX idx_classifications_message ON classifications(gmail_message_id);
    CREATE VIEW current_classifications AS
        SELECT c.* FROM classifications c
        WHERE c.id = (
            SELECT MAX(c2.id) FROM classifications c2
            WHERE c2.gmail_message_id = c.gmail_message_id
        );
    """,
    """
    -- v5 -> v6: messages.origin (follow-up). A backfilled message is
    -- inserted (by _submit_page) before its Batch API result is known, and an
    -- expired/canceled result deliberately leaves no classification row so a
    -- later page can retry it (the locked failure policy). Without a way to
    -- tell "backfill's, still pending" apart from "genuinely new", the live
    -- `assistant run` loop's classification-row-presence check mistook that
    -- pending row for fresh mail and reclassified it through the live path —
    -- Gmail priority label + P1 Telegram ping — for old historical mail. That
    -- violates the actionable-set isolation the ticket requires regardless of
    -- classification status, so gate on origin directly: the live loop now
    -- excludes origin != 'poll' unconditionally (cli._messages_needing_classification).
    -- DEFAULT 'poll' means poll.py's existing INSERT needs no change.
    ALTER TABLE messages ADD COLUMN origin TEXT NOT NULL DEFAULT 'poll'
        CHECK(origin IN ('poll','backfill'));
    """,
    """
    -- v6 -> v7: FTS5 search index over messages. External-
    -- content mode (content='messages') avoids duplicating sender/subject/body
    -- text, but needs a stable INTEGER content_rowid — messages' own PK is TEXT
    -- (gmail_message_id), and its implicit rowid isn't safe to use directly (a
    -- VACUUM can renumber it, silently desyncing the index). fts_rowid snapshots
    -- the rowid at insert time into a real column instead.
    ALTER TABLE messages ADD COLUMN fts_rowid INTEGER;
    UPDATE messages SET fts_rowid = rowid;
    CREATE UNIQUE INDEX idx_messages_fts_rowid ON messages(fts_rowid);

    CREATE VIRTUAL TABLE messages_fts USING fts5(
        sender, subject, body,
        content='messages', content_rowid='fts_rowid',
        tokenize='porter unicode61'
    );
    INSERT INTO messages_fts(rowid, sender, subject, body)
        SELECT fts_rowid, sender, subject, body FROM messages;

    -- New rows only: SQLite can't mutate the row being inserted from a BEFORE
    -- trigger, so this has to be AFTER, where NEW.rowid is already assigned.
    -- WHEN NEW.fts_rowid IS NULL means app inserts (which never set the
    -- column) fire this; it never re-fires for rows this migration backfilled.
    CREATE TRIGGER trg_messages_fts_insert AFTER INSERT ON messages
    WHEN NEW.fts_rowid IS NULL
    BEGIN
        UPDATE messages SET fts_rowid = NEW.rowid
            WHERE gmail_message_id = NEW.gmail_message_id;
        INSERT INTO messages_fts(rowid, sender, subject, body)
            VALUES (NEW.rowid, NEW.sender, NEW.subject, NEW.body);
    END;
    """,
]

# Derived, not hardcoded: a literal constant here has twice drifted out of sync
# with len(_MIGRATIONS) when a migration was appended without updating it too.
SCHEMA_VERSION = len(_MIGRATIONS)


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
