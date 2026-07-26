"""Store: schema creation, versioning, pragmas, append-only event semantics."""

import sqlite3

import pytest

from assistant.store import SCHEMA_VERSION, new_id, now_iso, open_db

TABLES = {"messages", "llm_calls", "classifications", "action_events", "run_events"}
VIEWS = {"current_actions", "current_checkpoint", "current_classifications"}


def _add_message(conn, mid="m1", sender=None, subject=None, body=None):
    conn.execute(
        "INSERT INTO messages(gmail_message_id, sender, subject, body, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (mid, sender, subject, body, now_iso()),
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


def _classify(conn, mid, category, source=None):
    cols = "gmail_message_id, category, classified_at"
    vals = [mid, category, now_iso()]
    if source is not None:
        cols += ", source"
        vals.append(source)
    conn.execute(
        f"INSERT INTO classifications({cols}) VALUES ({','.join('?' * len(vals))})",
        vals,
    )
    conn.commit()


def test_source_defaults_to_worker(tmp_path):
    # v4: an insert that omits `source` (the classifier path) defaults to 'worker'.
    conn = open_db(tmp_path / "triage.db")
    _add_message(conn)
    _classify(conn, "m1", "Work")
    assert (
        conn.execute("SELECT source FROM classifications").fetchone()["source"]
        == "worker"
    )


def test_source_check_rejects_unknown_value(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    _add_message(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _classify(conn, "m1", "Work", source="hermes")  # not in the CHECK set


def test_current_classifications_exposes_source(tmp_path):
    # The `SELECT c.*` view carries the new column through unchanged.
    conn = open_db(tmp_path / "triage.db")
    _add_message(conn)
    _classify(conn, "m1", "Work")  # worker
    _classify(conn, "m1", "Personal", source="human-chat")  # supersedes
    row = conn.execute(
        "SELECT category, source FROM current_classifications WHERE gmail_message_id='m1'"
    ).fetchone()
    assert (row["category"], row["source"]) == ("Personal", "human-chat")


def _fts_search(conn, query):
    rows = conn.execute(
        "SELECT m.gmail_message_id FROM messages_fts "
        "JOIN messages m ON m.fts_rowid = messages_fts.rowid "
        "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts, 5.0, 5.0, 1.0)",
        (query,),
    ).fetchall()
    return [r["gmail_message_id"] for r in rows]


def test_new_message_is_searchable_immediately(tmp_path):
    # No separate reindex step — the AFTER INSERT trigger syncs it inline.
    conn = open_db(tmp_path / "triage.db")
    _add_message(
        conn, sender="billing@bank.example.com", subject="Invoice", body="due soon"
    )
    conn.commit()
    assert _fts_search(conn, "invoice") == ["m1"]


def test_stemmed_query_matches(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    _add_message(conn, body="Your payments this month totaled $50")
    conn.commit()
    assert _fts_search(conn, "payment") == ["m1"]  # stems to 'payments' in body


def test_sender_subject_body_all_searchable(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    _add_message(conn, mid="a", sender="rao.office@clinic.example.com")
    _add_message(conn, mid="b", subject="lease renewal decision")
    _add_message(conn, mid="c", body="benefits enrollment form attached")
    conn.commit()
    assert _fts_search(conn, "clinic") == ["a"]
    assert _fts_search(conn, "lease") == ["b"]
    assert _fts_search(conn, "enrollment") == ["c"]


def test_duplicate_insert_does_not_error_or_duplicate_fts_row(tmp_path):
    conn = open_db(tmp_path / "triage.db")
    _add_message(conn, sender="original")
    conn.commit()
    conn.execute(
        "INSERT OR IGNORE INTO messages(gmail_message_id, sender, first_seen_at) "
        "VALUES ('m1', 'dup', ?)",
        (now_iso(),),
    )
    conn.commit()
    assert conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 1
    assert conn.execute("SELECT sender FROM messages").fetchone()[0] == "original"
