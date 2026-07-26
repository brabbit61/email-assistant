"""Calendar verbs: slot search, marker enforcement, audit trail.

Hand-rolled fake Calendar service (freebusy/events), no network — mirrors
test_apply.py / test_draft.py's style. Real tmp SQLite via store.open_db().
"""

from datetime import datetime

import pytest

from assistant import calendar, store


class _Exec:
    def __init__(self, result, fail=False):
        self._result, self._fail = result, fail

    def execute(self):
        if self._fail:
            raise RuntimeError("calendar outage")
        return self._result


class FakeService:
    def __init__(
        self,
        busy=None,
        events=None,
        fail_insert=False,
        fail_patch=False,
        fail_delete=False,
    ):
        self.busy = busy or []
        self.events_db = dict(events or {})
        self.calls: list[tuple[str, dict]] = []
        self._fail_insert = fail_insert
        self._fail_patch = fail_patch
        self._fail_delete = fail_delete
        self._next_id = 1

    def freebusy(self):
        return self

    def query(self, body):
        self.calls.append(("freebusy.query", body))
        return _Exec({"calendars": {"primary": {"busy": self.busy}}})

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.calls.append(("events.insert", body))
        event_id = f"evt{self._next_id}"
        self._next_id += 1
        self.events_db[event_id] = {"id": event_id, **body}
        return _Exec({"id": event_id, **body}, fail=self._fail_insert)

    def get(self, calendarId, eventId):
        self.calls.append(("events.get", {"eventId": eventId}))
        event = self.events_db.get(eventId)
        return _Exec(event, fail=event is None)

    def patch(self, calendarId, eventId, body):
        self.calls.append(("events.patch", {"eventId": eventId, **body}))
        return _Exec({}, fail=self._fail_patch)

    def delete(self, calendarId, eventId):
        self.calls.append(("events.delete", {"eventId": eventId}))
        return _Exec({}, fail=self._fail_delete)


def _owned_event(event_id, start, end):
    return {
        "id": event_id,
        "start": {"dateTime": start, "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end, "timeZone": "America/Los_Angeles"},
        "extendedProperties": {
            "private": {
                "assistant": "email-assistant",
                "source_gmail_message_id": "msg1",
            }
        },
    }


def _fresh_conn(tmp_path):
    conn = store.open_db(tmp_path / "triage.db")
    conn.execute(
        "INSERT INTO messages(gmail_message_id, first_seen_at) VALUES ('msg1', ?)",
        (store.now_iso(),),
    )
    conn.commit()
    return conn


# --- slots ---------------------------------------------------------------


def test_slots_excludes_busy_and_filters_short_gaps():
    svc = FakeService(
        busy=[
            {"start": "2026-07-27T12:00:00-07:00", "end": "2026-07-27T13:00:00-07:00"},
        ]
    )
    gaps = calendar.free_slots(svc, "2026-07-27T09:00:00", "2026-07-27T17:00:00", 60)
    assert gaps == [
        (datetime(2026, 7, 27, 9, 0), datetime(2026, 7, 27, 12, 0)),
        (datetime(2026, 7, 27, 13, 0), datetime(2026, 7, 27, 17, 0)),
    ]

    # 200 min fits the 4h afternoon gap (240m) but not the 3h morning gap (180m).
    gaps_200 = calendar.free_slots(
        svc, "2026-07-27T09:00:00", "2026-07-27T17:00:00", 200
    )
    assert gaps_200 == [(datetime(2026, 7, 27, 13, 0), datetime(2026, 7, 27, 17, 0))]


def test_slots_dst_boundary_uses_real_elapsed_time():
    """Spring-forward 2026-03-08: 00:00-06:00 Pacific wall-clock is only 5 real
    hours (the 2am hour doesn't exist), not 6 — proves the duration filter uses
    aware (zoneinfo) arithmetic, not naive wall-clock subtraction."""
    svc = FakeService(busy=[])
    assert (
        len(calendar.free_slots(svc, "2026-03-08T00:00:00", "2026-03-08T06:00:00", 300))
        == 1
    )
    assert (
        calendar.free_slots(svc, "2026-03-08T00:00:00", "2026-03-08T06:00:00", 301)
        == []
    )


def test_slots_rejects_offset_aware_input():
    with pytest.raises(ValueError, match="offset-aware"):
        calendar.free_slots(
            FakeService(), "2026-07-27T09:00:00-07:00", "2026-07-27T17:00:00", 30
        )


# --- create ----------------------------------------------------------------


def test_create_writes_marker_and_audit_pair(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService()

    result = calendar.create_event(
        conn,
        svc,
        "run1",
        start="2026-07-28T18:00:00",
        duration_minutes=15,
        title="Test",
        description="desc text",
        gmail_message_id="msg1",
    )

    assert result.event_id == "evt1"
    assert result.start == "2026-07-28T18:00:00"
    assert result.end == "2026-07-28T18:15:00"

    insert_calls = [c for c in svc.calls if c[0] == "events.insert"]
    assert len(insert_calls) == 1
    body = insert_calls[0][1]
    assert body["description"] == "desc text"
    assert body["extendedProperties"]["private"] == {
        "assistant": "email-assistant",
        "source_gmail_message_id": "msg1",
    }

    rows = conn.execute(
        "SELECT status, action_type, gmail_message_id FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("intended", "calendar_create", "msg1"),
        ("confirmed", "calendar_create", "msg1"),
    ]


def test_create_unknown_message_id_raises_before_any_write(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService()

    with pytest.raises(ValueError, match="unknown message id"):
        calendar.create_event(
            conn,
            svc,
            "run1",
            start="2026-07-28T18:00:00",
            duration_minutes=15,
            title="Test",
            description="desc",
            gmail_message_id="ghost",
        )

    assert svc.calls == []
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_create_api_failure_records_failed_and_reraises(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(fail_insert=True)

    with pytest.raises(RuntimeError):
        calendar.create_event(
            conn,
            svc,
            "run1",
            start="2026-07-28T18:00:00",
            duration_minutes=15,
            title="Test",
            description="desc",
            gmail_message_id="msg1",
        )

    rows = conn.execute(
        "SELECT status, action_type FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("intended", "calendar_create"),
        ("failed", "calendar_create"),
    ]


# --- move --------------------------------------------------------------------


def test_move_preserves_duration_and_writes_audit(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(
        events={
            "evt1": _owned_event(
                "evt1", "2026-07-28T18:00:00-07:00", "2026-07-28T18:15:00-07:00"
            )
        }
    )

    result = calendar.move_event(conn, svc, "run1", "evt1", "2026-07-29T09:00:00")

    assert result.start == "2026-07-29T09:00:00"
    assert result.end == "2026-07-29T09:15:00"  # 15-minute duration preserved
    assert [c[0] for c in svc.calls] == ["events.get", "events.patch"]

    rows = conn.execute(
        "SELECT status, action_type, gmail_message_id FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("intended", "calendar_move", None),
        ("confirmed", "calendar_move", None),
    ]


def test_move_refuses_non_agent_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(
        events={
            "evt2": {
                "id": "evt2",
                "start": {"dateTime": "2026-07-28T18:00:00-07:00"},
                "end": {"dateTime": "2026-07-28T18:15:00-07:00"},
            }  # no ownership marker
        }
    )

    with pytest.raises(ValueError, match="not created by this assistant"):
        calendar.move_event(conn, svc, "run1", "evt2", "2026-07-29T09:00:00")

    assert [c[0] for c in svc.calls] == ["events.get"]  # no patch call
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_move_unknown_event_raises(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(events={})

    with pytest.raises(ValueError, match="could not read event"):
        calendar.move_event(conn, svc, "run1", "ghost", "2026-07-29T09:00:00")

    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_move_api_failure_records_failed_and_reraises(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(
        events={
            "evt1": _owned_event(
                "evt1", "2026-07-28T18:00:00-07:00", "2026-07-28T18:15:00-07:00"
            )
        },
        fail_patch=True,
    )

    with pytest.raises(RuntimeError):
        calendar.move_event(conn, svc, "run1", "evt1", "2026-07-29T09:00:00")

    rows = conn.execute(
        "SELECT status, action_type FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("intended", "calendar_move"),
        ("failed", "calendar_move"),
    ]


# --- delete --------------------------------------------------------------------


def test_delete_succeeds_on_owned_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(
        events={
            "evt1": _owned_event(
                "evt1", "2026-07-28T18:00:00-07:00", "2026-07-28T18:15:00-07:00"
            )
        }
    )

    calendar.delete_event(conn, svc, "run1", "evt1")

    assert [c[0] for c in svc.calls] == ["events.get", "events.delete"]
    rows = conn.execute(
        "SELECT status, action_type FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("intended", "calendar_delete"),
        ("confirmed", "calendar_delete"),
    ]


def test_delete_refuses_non_agent_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(events={"evt2": {"id": "evt2"}})  # no extendedProperties at all

    with pytest.raises(ValueError, match="not created by this assistant"):
        calendar.delete_event(conn, svc, "run1", "evt2")

    assert [c[0] for c in svc.calls] == ["events.get"]
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_delete_api_failure_records_failed_and_reraises(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(
        events={
            "evt1": _owned_event(
                "evt1", "2026-07-28T18:00:00-07:00", "2026-07-28T18:15:00-07:00"
            )
        },
        fail_delete=True,
    )

    with pytest.raises(RuntimeError):
        calendar.delete_event(conn, svc, "run1", "evt1")

    rows = conn.execute(
        "SELECT status, action_type FROM action_events ORDER BY event_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("intended", "calendar_delete"),
        ("failed", "calendar_delete"),
    ]
