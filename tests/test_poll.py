"""T1.5 poller: incremental ingest, idempotency, 404 catch-up sweep, cold start.

Fakes at the `gmail` helper seam (monkeypatch) against a real tmp SQLite — no
Gmail client, no network. Mirrors test_gmail.py's hand-rolled-fake style.
"""

from googleapiclient.errors import HttpError

from assistant import gmail, poll, store

SVC = object()  # opaque: every gmail helper is monkeypatched, so svc is unused


class FakeResp(dict):
    """Minimal httplib2-style response so HttpError(...).resp.status works."""

    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = "Not Found"


def _msg(mid, body="hello"):
    return {
        "gmail_message_id": mid,
        "thread_id": "t" + mid,
        "sender": "a@b.com",
        "subject": "subj " + mid,
        "body": body,
        "internal_date_ms": 123,
        "gmail_label_ids": '["INBOX", "IMPORTANT"]',
        "raw_json": '{"id": "' + mid + '"}',
    }


def _seed_checkpoint(conn, history_id="100", recorded_at=None):
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, history_id, recorded_at) "
        "VALUES (?, 'finished', 'ok', ?, ?)",
        (store.new_id(), history_id, recorded_at or store.now_iso()),
    )
    conn.commit()


def _checkpoint(conn):
    row = conn.execute("SELECT history_id FROM current_checkpoint").fetchone()
    return row["history_id"] if row else None


def _msg_count(conn):
    return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_incremental_ingests_and_advances_checkpoint(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_checkpoint(conn, history_id="100")
    monkeypatch.setattr(gmail, "iter_history", lambda svc, s: (["m1", "m2"], "200"))
    monkeypatch.setattr(gmail, "get_message", lambda svc, mid: _msg(mid, body=mid))

    result = poll.poll_once(conn, SVC)

    assert (result.messages_seen, result.inserted, result.catchup) == (2, 2, False)
    assert _msg_count(conn) == 2
    assert _checkpoint(conn) == "200"  # advanced to the history.list response id
    bodies = {
        r["gmail_message_id"]: r["body"]
        for r in conn.execute("SELECT gmail_message_id, body FROM messages")
    }
    assert bodies == {"m1": "m1", "m2": "m2"}
    # Gmail's auto-labels + raw response persisted at ingestion (new columns)
    row = conn.execute(
        "SELECT gmail_label_ids, raw_json FROM messages WHERE gmail_message_id='m1'"
    ).fetchone()
    assert row["gmail_label_ids"] == '["INBOX", "IMPORTANT"]'
    assert row["raw_json"] == '{"id": "m1"}'


def test_replay_is_idempotent(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_checkpoint(conn, history_id="100")
    monkeypatch.setattr(gmail, "iter_history", lambda svc, s: (["m1", "m2"], "200"))
    monkeypatch.setattr(gmail, "get_message", lambda svc, mid: _msg(mid))

    poll.poll_once(conn, SVC)
    # Same window replayed (e.g. after a crash before the checkpoint advanced).
    monkeypatch.setattr(gmail, "iter_history", lambda svc, s: (["m1", "m2"], "200"))
    result = poll.poll_once(conn, SVC)

    assert result.inserted == 0  # INSERT OR IGNORE against the PK
    assert _msg_count(conn) == 2


def test_expired_historyid_triggers_bounded_sweep(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_checkpoint(conn, history_id="100", recorded_at="2026-07-01T00:00:00Z")

    def _raise_404(svc, start_id):
        raise HttpError(FakeResp(404), b"{}")

    seen_since = {}

    def _sweep(svc, epoch_s):
        seen_since["epoch"] = epoch_s
        return ["m9"]

    monkeypatch.setattr(gmail, "iter_history", _raise_404)
    monkeypatch.setattr(gmail, "list_messages_since", _sweep)
    monkeypatch.setattr(gmail, "current_history_id", lambda svc: "300")
    monkeypatch.setattr(gmail, "get_message", lambda svc, mid: _msg(mid))

    result = poll.poll_once(conn, SVC)

    assert result.catchup is True
    assert result.inserted == 1
    assert seen_since["epoch"] == 1782864000  # 2026-07-01T00:00:00Z in Unix seconds
    assert _checkpoint(conn) == "300"  # fresh bootstrap id after the sweep
    gap = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE phase='catchup' AND status='gap'"
    ).fetchone()[0]
    assert gap == 1  # loud, auditable gap marker


def test_cold_start_bootstraps_forward_only(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")  # no checkpoint at all

    def _fail(*a, **k):
        raise AssertionError("cold start must not call history.list")

    monkeypatch.setattr(gmail, "iter_history", _fail)
    monkeypatch.setattr(gmail, "current_history_id", lambda svc: "500")

    result = poll.poll_once(conn, SVC)

    assert (result.inserted, result.messages_seen) == (0, 0)
    assert (
        _msg_count(conn) == 0
    )  # pre-existing mail is backfill's job, not the poller's
    assert _checkpoint(conn) == "500"
