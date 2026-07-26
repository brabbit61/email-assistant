"""Create-draft: reply-all resolution, threading, quote-back, audit.

Hand-rolled fake Gmail service (getProfile, drafts().create) against a real
tmp SQLite — mirrors test_apply.py / test_correct.py's style. No network.
"""

import json

import pytest

from assistant import draft, store

MY_EMAIL = "alex@example.com"


class _Exec:
    def __init__(self, result, fail=False):
        self._result, self._fail = result, fail

    def execute(self):
        if self._fail:
            raise RuntimeError("gmail outage")
        return self._result


class FakeService:
    def __init__(self, my_email=MY_EMAIL, fail_create=False):
        self.my_email = my_email
        self.calls: list[dict] = []
        self._fail_create = fail_create

    def users(self):
        return self

    def getProfile(self, userId):
        return _Exec({"emailAddress": self.my_email})

    def drafts(self):
        return self

    def create(self, userId, body):
        self.calls.append(body)
        return _Exec({"id": "draft123"}, fail=self._fail_create)


def _headers_json(headers: dict[str, str]) -> str:
    return json.dumps(
        {"payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]}}
    )


def _insert(
    conn, mid, thread_id, sender, subject, body, internal_date_ms, headers=None
):
    conn.execute(
        "INSERT INTO messages"
        "(gmail_message_id, thread_id, sender, subject, body, internal_date_ms, "
        " raw_json, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mid,
            thread_id,
            sender,
            subject,
            body,
            internal_date_ms,
            _headers_json(headers or {}),
            store.now_iso(),
        ),
    )
    conn.commit()


def _conn(tmp_path):
    return store.open_db(tmp_path / "triage.db")


def _seed_basic_thread(conn):
    _insert(
        conn,
        "m1",
        "t1",
        "Alice Smith <alice@example.com>",
        "Project update",
        "Hi Alex,\nHere's the update.\nThanks,\nAlice",
        1721577900000,  # 2024-07-21T15:45:00Z-ish, exact TZ doesn't matter for tests
        headers={
            "Message-ID": "<orig@example.com>",
            "To": "Alex Doe <alex@example.com>, Bob Lee <bob@example.com>",
            "Cc": "Carol Diaz <carol@example.com>",
        },
    )


def test_reply_all_recipients_exclude_self_and_dedupe(tmp_path):
    conn = _conn(tmp_path)
    _seed_basic_thread(conn)
    svc = FakeService()

    result = draft.create_draft_reply(conn, svc, "t1", "Thanks, will do.")

    assert result.to == "Alice Smith <alice@example.com>"
    assert result.cc == ["Bob Lee <bob@example.com>", "Carol Diaz <carol@example.com>"]
    body = svc.calls[0]["message"]
    assert body["threadId"] == "t1"


def test_threading_headers_set_from_message_id_and_references(tmp_path):
    conn = _conn(tmp_path)
    _seed_basic_thread(conn)
    svc = FakeService()

    draft.create_draft_reply(conn, svc, "t1", "Thanks, will do.")

    import base64

    raw = base64.urlsafe_b64decode(svc.calls[0]["message"]["raw"]).decode()
    assert "In-Reply-To: <orig@example.com>" in raw
    assert "References: <orig@example.com>" in raw


def test_quote_back_includes_composed_text_and_original(tmp_path):
    conn = _conn(tmp_path)
    _seed_basic_thread(conn)
    svc = FakeService()

    import base64

    draft.create_draft_reply(conn, svc, "t1", "Thanks, will do.")
    raw = base64.urlsafe_b64decode(svc.calls[0]["message"]["raw"]).decode()

    assert "Thanks, will do." in raw
    assert "Alice Smith <alice@example.com> wrote:" in raw
    assert "> Hi Alex," in raw
    assert "> Here's the update." in raw


def test_subject_gets_re_prefix_once(tmp_path):
    conn = _conn(tmp_path)
    _seed_basic_thread(conn)
    svc = FakeService()

    import base64

    draft.create_draft_reply(conn, svc, "t1", "Thanks.")
    raw = base64.urlsafe_b64decode(svc.calls[0]["message"]["raw"]).decode()
    assert "Subject: Re: Project update" in raw

    # already-prefixed subject isn't doubled
    conn2 = _conn(tmp_path)
    _insert(
        conn2,
        "m2",
        "t2",
        "Alice <alice@example.com>",
        "Re: Project update",
        "body",
        1721577900000,
    )
    svc2 = FakeService()
    draft.create_draft_reply(conn2, svc2, "t2", "Thanks.")
    raw2 = base64.urlsafe_b64decode(svc2.calls[0]["message"]["raw"]).decode()
    assert "Subject: Re: Re:" not in raw2


def test_missing_message_id_falls_back_to_threadid_only(tmp_path):
    conn = _conn(tmp_path)
    _insert(
        conn,
        "m1",
        "t1",
        "Alice <alice@example.com>",
        "Hi",
        "body",
        1721577900000,
        headers={},  # no Message-ID header at all
    )
    svc = FakeService()

    result = draft.create_draft_reply(conn, svc, "t1", "Thanks.")

    import base64

    raw = base64.urlsafe_b64decode(svc.calls[0]["message"]["raw"]).decode()
    assert "In-Reply-To" not in raw
    assert result.draft_id == "draft123"


def test_audit_intended_then_confirmed_on_success(tmp_path):
    conn = _conn(tmp_path)
    _seed_basic_thread(conn)
    svc = FakeService()

    draft.create_draft_reply(conn, svc, "t1", "Thanks, will do.")

    rows = conn.execute(
        "SELECT status, action_type, actor, gmail_message_id, thread_id "
        "FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [r["status"] for r in rows] == ["intended", "confirmed"]
    assert all(r["action_type"] == "draft" for r in rows)
    assert all(r["actor"] == "agent" for r in rows)
    assert all(r["gmail_message_id"] == "m1" for r in rows)
    assert all(r["thread_id"] == "t1" for r in rows)


def test_gmail_failure_records_failed_and_reraises(tmp_path):
    conn = _conn(tmp_path)
    _seed_basic_thread(conn)
    svc = FakeService(fail_create=True)

    with pytest.raises(RuntimeError, match="gmail outage"):
        draft.create_draft_reply(conn, svc, "t1", "Thanks, will do.")

    rows = conn.execute(
        "SELECT status, error FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [r["status"] for r in rows] == ["intended", "failed"]
    assert "gmail outage" in rows[1]["error"]


def test_empty_body_raises_before_any_write(tmp_path):
    conn = _conn(tmp_path)
    _seed_basic_thread(conn)
    svc = FakeService()

    with pytest.raises(ValueError, match="empty"):
        draft.create_draft_reply(conn, svc, "t1", "   ")

    assert svc.calls == []
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_unknown_thread_raises(tmp_path):
    conn = _conn(tmp_path)
    svc = FakeService()

    with pytest.raises(ValueError, match="no messages found"):
        draft.create_draft_reply(conn, svc, "nope", "Thanks.")

    assert svc.calls == []


def test_thread_with_only_own_messages_raises(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "m1", "t1", "Alex Doe <alex@example.com>", "Fwd", "body", 1)
    svc = FakeService()

    with pytest.raises(ValueError, match="no inbound message"):
        draft.create_draft_reply(conn, svc, "t1", "Thanks.")

    assert svc.calls == []


def test_latest_inbound_message_picked_over_older_ones(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "m1", "t1", "Alice <alice@example.com>", "Hi", "old body", 1000)
    _insert(conn, "m2", "t1", "Alex Doe <alex@example.com>", "Re: Hi", "my reply", 2000)
    _insert(
        conn, "m3", "t1", "Alice <alice@example.com>", "Re: Hi", "newest body", 3000
    )
    svc = FakeService()

    draft.create_draft_reply(conn, svc, "t1", "Thanks.")

    rows = conn.execute(
        "SELECT gmail_message_id FROM action_events WHERE status='intended'"
    ).fetchall()
    assert rows[0]["gmail_message_id"] == "m3"
