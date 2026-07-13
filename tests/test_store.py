"""T1.2 store: schema creation, versioning, pragmas, append-only event semantics."""

import sqlite3

import pytest

from assistant.store import SCHEMA_VERSION, new_id, now_iso, open_db

TABLES = {"messages", "llm_calls", "classifications", "action_events", "run_events"}
VIEWS = {"current_actions", "current_checkpoint", "current_classifications"}


def _add_message(conn, mid="m1"):
    conn.execute(
        "INSERT INTO messages(gmail_message_id, first_seen_at) VALUES (?, ?)",
        (mid, now_iso()),
    )


def test_open_creates_db_and_schema(tmp_path):
    db = tmp_path / "sub" / "triage.db"
    conn = open_db(db)  # nested dir must be created automatically
    assert db.exists()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    views = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
    }
    assert TABLES <= names
    assert VIEWS == views


def test_pragmas_set(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "triage.db"
    open_db(db).close()
    conn = open_db(db)  # second open must not re-run or error
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_action_lifecycle_derives_current_state(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    _add_message(conn)
    action_id = new_id()
    # intended appended before the (hypothetical) call, confirmed after — same action_id
    for status in ("intended", "confirmed"):
        conn.execute(
            "INSERT INTO action_events(action_id, status, action_type, actor, "
            "gmail_message_id, recorded_at) VALUES (?, ?, 'label_add', 'worker', 'm1', ?)",
            (action_id, status, now_iso()),
        )
    conn.commit()
    rows = conn.execute("SELECT status FROM current_actions").fetchall()
    assert len(rows) == 1  # one logical action
    assert rows[0]["status"] == "confirmed"  # latest event wins


def test_current_checkpoint_is_latest_ok_finish(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    for hist in ("100", "200"):
        rid = new_id()
        conn.execute(
            "INSERT INTO run_events(run_id, phase, recorded_at) VALUES (?, 'started', ?)",
            (rid, now_iso()),
        )
        conn.execute(
            "INSERT INTO run_events(run_id, phase, status, history_id, recorded_at) "
            "VALUES (?, 'finished', 'ok', ?, ?)",
            (rid, hist, now_iso()),
        )
    conn.commit()
    assert (
        conn.execute("SELECT history_id FROM current_checkpoint").fetchone()[0] == "200"
    )


def test_foreign_key_enforced(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO action_events(action_id, status, action_type, actor, "
            "gmail_message_id, recorded_at) VALUES (?, 'intended', 'label_add', "
            "'worker', 'no-such-message', ?)",
            (new_id(), now_iso()),
        )
