"""Poller: incremental ingest, idempotency, 404 catch-up sweep, cold start.

Fakes at the `gmail` helper seam (monkeypatch) against a real tmp SQLite — no
Gmail client, no network. Mirrors test_gmail.py's hand-rolled-fake style.
"""

from googleapiclient.errors import HttpError

from assistant import correct, gmail, poll, store

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
    monkeypatch.setattr(gmail, "iter_history", lambda svc, s: (["m1", "m2"], [], "200"))
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
    monkeypatch.setattr(gmail, "iter_history", lambda svc, s: (["m1", "m2"], [], "200"))
    monkeypatch.setattr(gmail, "get_message", lambda svc, mid: _msg(mid))

    poll.poll_once(conn, SVC)
    # Same window replayed (e.g. after a crash before the checkpoint advanced).
    monkeypatch.setattr(gmail, "iter_history", lambda svc, s: (["m1", "m2"], [], "200"))
    result = poll.poll_once(conn, SVC)

    assert result.inserted == 0  # INSERT OR IGNORE against the PK
    assert _msg_count(conn) == 2


def test_expired_historyid_triggers_bounded_sweep(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_checkpoint(conn, history_id="100", recorded_at="2026-07-01T00:00:00Z")

    def _raise_404(svc, start_id):
        raise HttpError(FakeResp(404), b"{}")

    seen_since = {}

    def _sweep(svc, q):
        seen_since["query"] = q
        return ["m9"]

    monkeypatch.setattr(gmail, "iter_history", _raise_404)
    monkeypatch.setattr(gmail, "list_message_ids", _sweep)
    monkeypatch.setattr(gmail, "current_history_id", lambda svc: "300")
    monkeypatch.setattr(gmail, "get_message", lambda svc, mid: _msg(mid))

    result = poll.poll_once(conn, SVC)

    assert result.catchup is True
    assert result.inserted == 1
    assert seen_since["query"] == "in:inbox after:1782864000"  # 2026-07-01T00:00:00Z
    assert _checkpoint(conn) == "300"  # fresh bootstrap id after the sweep
    gap = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE phase='catchup' AND status='gap'"
    ).fetchone()[0]
    assert gap == 1  # loud, auditable gap marker


def test_deleted_message_is_skipped_not_fatal(tmp_path, monkeypatch):
    # A message can be listed in history and then permanently deleted before
    # the full fetch (get_message 404s). That must not crash the whole run —
    # crashing here means the checkpoint never advances, so the next tick
    # re-lists the same window, hits the same 404, forever (a permanent wedge).
    conn = store.open_db(tmp_path / "triage.db")
    _seed_checkpoint(conn, history_id="100")
    monkeypatch.setattr(
        gmail, "iter_history", lambda svc, s: (["gone", "m2"], [], "200")
    )

    def _get(svc, mid):
        if mid == "gone":
            raise HttpError(FakeResp(404), b"{}")
        return _msg(mid)

    monkeypatch.setattr(gmail, "get_message", _get)

    result = poll.poll_once(conn, SVC)

    assert result.inserted == 1
    assert _msg_count(conn) == 1
    assert conn.execute("SELECT 1 FROM messages WHERE gmail_message_id='m2'").fetchone()
    assert _checkpoint(conn) == "200"  # still advances past the deleted message


def test_relabel_events_forwarded_before_checkpoint(tmp_path, monkeypatch):
    # Flow A: poll_once hands label events to correct.detect_relabels, and
    # does so *before* it writes the 'finished' checkpoint row (same crash-safety
    # ordering as message ingest).
    conn = store.open_db(tmp_path / "triage.db")
    _seed_checkpoint(conn, history_id="100")
    events = [("m1", frozenset({"Label_9"}))]
    monkeypatch.setattr(gmail, "iter_history", lambda svc, s: (["m1"], events, "200"))
    monkeypatch.setattr(gmail, "get_message", lambda svc, mid: _msg(mid))

    captured = {}

    def _detect(c, svc, label_events):
        captured["events"] = label_events
        captured["finished_rows_at_call"] = c.execute(
            "SELECT COUNT(*) FROM run_events WHERE phase='finished'"
        ).fetchone()[0]
        return 0

    monkeypatch.setattr(correct, "detect_relabels", _detect)

    before = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE phase='finished'"
    ).fetchone()[0]
    poll.poll_once(conn, SVC)

    assert captured["events"] == events  # events forwarded verbatim
    # This poll's 'finished' row isn't written yet when detection runs.
    assert captured["finished_rows_at_call"] == before


def test_catchup_forwards_no_relabel_events(tmp_path, monkeypatch):
    # The 404 catch-up sweep carries no history, so no label events reach detection.
    conn = store.open_db(tmp_path / "triage.db")
    _seed_checkpoint(conn, history_id="100", recorded_at="2026-07-01T00:00:00Z")

    def _raise_404(svc, start_id):
        raise HttpError(FakeResp(404), b"{}")

    monkeypatch.setattr(gmail, "iter_history", _raise_404)
    monkeypatch.setattr(gmail, "list_message_ids", lambda svc, q: [])
    monkeypatch.setattr(gmail, "current_history_id", lambda svc: "300")
    seen = {}

    def _detect(c, svc, ev):
        seen["events"] = ev
        return 0

    monkeypatch.setattr(correct, "detect_relabels", _detect)

    poll.poll_once(conn, SVC)

    assert seen["events"] == []


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
