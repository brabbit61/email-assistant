"""T2.2 Telegram P1 pings: dedupe, burst-collapse, audit-before-write, failure
isolation from run health (formats/triggers signed off in S2.2, #38).

Fakes at the send seam (a plain injected callable, no network) against a real
tmp SQLite via store.open_db() — mirrors test_apply.py's style.
"""

import json
import urllib.error

import pytest

from assistant import store, telegram
from assistant.classify import Verdict


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_send_posts_json_to_the_right_bot_url(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _FakeResponse(200)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    telegram.send("tok123", "chat1", "hello")

    assert captured["url"] == "https://api.telegram.org/bottok123/sendMessage"
    assert captured["body"] == {"chat_id": "chat1", "text": "hello"}
    assert captured["timeout"] == 10.0


def test_send_raises_on_non_2xx_status(monkeypatch):
    monkeypatch.setattr(
        telegram.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(500)
    )

    with pytest.raises(RuntimeError, match="500"):
        telegram.send("tok123", "chat1", "hello")


def test_send_propagates_network_errors(monkeypatch):
    def raise_url_error(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", raise_url_error)

    with pytest.raises(urllib.error.URLError):
        telegram.send("tok123", "chat1", "hello")


def _fresh_conn(tmp_path, ids=("m1", "m2", "m3")):
    conn = store.open_db(tmp_path / "triage.db")
    for mid in ids:
        conn.execute(
            "INSERT INTO messages(gmail_message_id, sender, subject, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (mid, f"sender-{mid}@example.com", f"subject {mid}", store.now_iso()),
        )
    conn.commit()
    return conn


def _row(conn, mid):
    return conn.execute(
        "SELECT * FROM messages WHERE gmail_message_id = ?", (mid,)
    ).fetchone()


def _fake_send(calls, fail=False):
    def send(token, chat_id, text):
        if fail:
            raise RuntimeError("telegram outage")
        calls.append((token, chat_id, text))

    return send


def test_single_p1_sends_one_message_and_confirms(tmp_path):
    conn = _fresh_conn(tmp_path)
    calls = []
    verdict = Verdict("Finance", "P1-Urgent", "failed autopay, bill due today")

    telegram.notify_p1(
        conn,
        "run1",
        "tok",
        "chat1",
        [("m1", _row(conn, "m1"), verdict)],
        send_fn=_fake_send(calls),
    )

    assert len(calls) == 1
    token, chat_id, text = calls[0]
    assert (token, chat_id) == ("tok", "chat1")
    assert "sender-m1@example.com" in text
    assert "failed autopay, bill due today" in text
    assert "subject m1" in text

    rows = conn.execute(
        "SELECT status FROM action_events WHERE action_type='telegram_ping'"
    ).fetchall()
    assert [r["status"] for r in rows] == ["intended", "confirmed"]


def test_burst_collapses_into_one_combined_message(tmp_path):
    conn = _fresh_conn(tmp_path)
    calls = []
    v = Verdict("Action-Needed", "P1-Urgent", "reply needed")
    hits = [(mid, _row(conn, mid), v) for mid in ("m1", "m2", "m3")]

    telegram.notify_p1(conn, "run1", "tok", "chat1", hits, send_fn=_fake_send(calls))

    assert len(calls) == 1  # one combined send, not three
    text = calls[0][2]
    assert "3 urgent" in text
    for mid in ("m1", "m2", "m3"):
        assert f"sender-{mid}@example.com" in text
    assert (
        "reply with a number" not in text.lower()
    )  # #42: dropped, worker can't act on it

    confirmed = conn.execute(
        "SELECT COUNT(*) FROM action_events WHERE action_type='telegram_ping' AND status='confirmed'"
    ).fetchone()[0]
    assert confirmed == 3  # each message still gets its own audit row


def test_dedupe_per_message_once_ever(tmp_path):
    conn = _fresh_conn(tmp_path)
    calls = []
    verdict = Verdict("Finance", "P1-Urgent", "wire confirmation needed")
    hit = ("m1", _row(conn, "m1"), verdict)

    telegram.notify_p1(conn, "run1", "tok", "chat1", [hit], send_fn=_fake_send(calls))
    telegram.notify_p1(conn, "run2", "tok", "chat1", [hit], send_fn=_fake_send(calls))

    assert len(calls) == 1  # second call is a no-op: already confirmed once


def test_new_p1_in_same_burst_as_already_notified_still_sends(tmp_path):
    conn = _fresh_conn(tmp_path)
    calls = []
    v = Verdict("Finance", "P1-Urgent", "urgent")
    telegram.notify_p1(
        conn,
        "run1",
        "tok",
        "chat1",
        [("m1", _row(conn, "m1"), v)],
        send_fn=_fake_send(calls),
    )

    hits = [("m1", _row(conn, "m1"), v), ("m2", _row(conn, "m2"), v)]
    telegram.notify_p1(conn, "run2", "tok", "chat1", hits, send_fn=_fake_send(calls))

    assert len(calls) == 2
    assert "sender-m1" not in calls[1][2]  # only the fresh one, m1 already notified
    assert "sender-m2" in calls[1][2]


def test_send_failure_is_isolated_never_raises(tmp_path):
    conn = _fresh_conn(tmp_path)
    verdict = Verdict("Finance", "P1-Urgent", "urgent")

    telegram.notify_p1(
        conn,
        "run1",
        "tok",
        "chat1",
        [("m1", _row(conn, "m1"), verdict)],
        send_fn=_fake_send([], fail=True),
    )  # must not raise

    rows = conn.execute(
        "SELECT status, error FROM action_events WHERE action_type='telegram_ping'"
    ).fetchall()
    assert [r["status"] for r in rows] == ["intended", "failed"]
    assert "telegram outage" in rows[1]["error"]
    # never wrote a run_events row of its own — notify_p1 doesn't touch run health
    assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0


def test_failed_send_leaves_message_eligible_for_retry_next_run(tmp_path):
    conn = _fresh_conn(tmp_path)
    verdict = Verdict("Finance", "P1-Urgent", "urgent")
    hit = ("m1", _row(conn, "m1"), verdict)

    telegram.notify_p1(
        conn, "run1", "tok", "chat1", [hit], send_fn=_fake_send([], fail=True)
    )

    calls = []
    telegram.notify_p1(conn, "run2", "tok", "chat1", [hit], send_fn=_fake_send(calls))

    assert len(calls) == 1  # only a *confirmed* send counts as notified


def test_no_hits_sends_nothing(tmp_path):
    conn = _fresh_conn(tmp_path)
    calls = []

    telegram.notify_p1(conn, "run1", "tok", "chat1", [], send_fn=_fake_send(calls))

    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0
