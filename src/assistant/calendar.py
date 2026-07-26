"""Calendar time-blocking verbs: slots / create / move / delete.

The CLI seam that lets the hermes agent search free/busy time and create, move,
or delete calendar events on the user's behalf, without the agent ever touching
the Calendar API directly. Same audit-before-write contract as `apply.py` /
`draft.py`: an `intended` `action_events` row before the Calendar API call,
`confirmed`/`failed` after. No `dry_run` — like `create_draft_reply` and
`apply_relabel`, these are explicit, user-initiated one-shot actions, not the
unattended run the go-live gate protects.

Write boundary (decision 7): the agent may move or delete an event
only if this code confirms it created that event, regardless of how the
request is phrased in chat. Ownership is a private extended property
(`MARKER_KEY`/`MARKER_VALUE`) written on `create` and checked by `_get_own_event`
before any mutating `move`/`delete` call — refusing loudly, with no API call
and no audit row, if it's missing. `SOURCE_KEY` also carries the originating
`gmail_message_id`, for the dedupe rule (an email with an existing future
block isn't re-proposed) — not read here.

OAuth grant (see `gmail.SCOPES`): `calendar.events` for create/move/delete —
broader than this boundary needs (a narrower `calendar.events.owned` would let
Google enforce "own events only" itself), but this code's marker check is the
actual enforcement, not the grant. `free_slots` additionally needs
`calendar.freebusy`: `calendar.events` does not cover `freebusy.query` even
though it covers `events().list` — confirmed empirically, not assumed.

Timezone comes from config (`[calendar] timezone`, default America/Los_Angeles):
`--start`/`--after`/`--before` are naive ISO timestamps interpreted as that
zone's wall-clock (DST-aware via stdlib `zoneinfo`). An offset-aware input is
rejected — silently assuming UTC would book the wrong wall-clock time.

`create`'s event description is written verbatim from the caller — the agent
composes the full format (sender, subject, deadline, action, Gmail
permalink); this module is a dumb writer, same division of labor as
`draft.py`.

ponytail: `events.insert` isn't idempotent like Gmail's `modify` or
`drafts.create` — a crash between the API call and the `confirmed` row can
leave a duplicate event on the calendar. Accepted (matches every other write's
posture here); add a client-set event id (409-on-retry) if a duplicate ever
actually bites.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

from assistant import store

DEFAULT_TIMEZONE = "America/Los_Angeles"

MARKER_KEY = "assistant"
MARKER_VALUE = "email-assistant"
SOURCE_KEY = "source_gmail_message_id"

_CALENDAR_ID = "primary"


@dataclass
class EventResult:
    event_id: str
    start: str  # naive local ISO
    end: str


def calendar_service(creds: Credentials) -> Resource:
    """Build the Calendar API client. Same token as Gmail."""
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def free_slots(
    svc: Resource,
    after: str,
    before: str,
    duration_minutes: int,
    tz_name: str = DEFAULT_TIMEZONE,
) -> list[tuple[datetime, datetime]]:
    """Maximal free gaps >= duration_minutes within [after, before) on the
    primary calendar, as naive local (start, end) pairs. Not a fixed-grid
    enumeration — keeps slot-selection defaults in the skill file, not
    here; the agent picks a start within a returned gap."""
    tz = ZoneInfo(tz_name)
    window_start = _parse_local(after).replace(tzinfo=tz)
    window_end = _parse_local(before).replace(tzinfo=tz)

    resp = (
        svc.freebusy()
        .query(
            body={
                "timeMin": window_start.isoformat(),
                "timeMax": window_end.isoformat(),
                "items": [{"id": _CALENDAR_ID}],
            }
        )
        .execute()
    )
    busy = sorted(
        (
            datetime.fromisoformat(b["start"]).astimezone(tz),
            datetime.fromisoformat(b["end"]).astimezone(tz),
        )
        for b in resp["calendars"][_CALENDAR_ID]["busy"]
    )

    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for busy_start, busy_end in busy:
        if busy_start > cursor:
            gaps.append((cursor, min(busy_start, window_end)))
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        gaps.append((cursor, window_end))

    # .timestamp() (true elapsed seconds), not `gap_end - gap_start`: aware
    # datetime subtraction takes a naive wall-clock shortcut whenever both
    # operands share a tzinfo *object* — which every ZoneInfo("...") call for
    # the same key does, via zoneinfo's own cache — silently dropping any DST
    # change inside the gap.
    duration_seconds = duration_minutes * 60
    return [
        (gap_start.replace(tzinfo=None), gap_end.replace(tzinfo=None))
        for gap_start, gap_end in gaps
        if gap_end.timestamp() - gap_start.timestamp() >= duration_seconds
    ]


def create_event(
    conn: sqlite3.Connection,
    svc: Resource,
    run_id: str,
    *,
    start: str,
    duration_minutes: int,
    title: str,
    description: str,
    gmail_message_id: str,
    tz_name: str = DEFAULT_TIMEZONE,
    actor: str = "agent",
) -> EventResult:
    """Create a marker-tagged event holding the source email's context. Raises
    ValueError before any Calendar call or audit write if gmail_message_id is
    unknown (never polled) — mirrors correct.py's preflight."""
    known = conn.execute(
        "SELECT 1 FROM messages WHERE gmail_message_id = ?", (gmail_message_id,)
    ).fetchone()
    if known is None:
        raise ValueError(f"unknown message id: {gmail_message_id}")

    start_local = _parse_local(start)
    end_local = start_local + timedelta(minutes=duration_minutes)

    action_id = store.new_id()
    detail = (
        f"start={start_local.isoformat()}; duration={duration_minutes}m; title={title}"
    )
    _record_intended(
        conn, action_id, "calendar_create", actor, run_id, gmail_message_id, detail
    )

    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_local.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_local.isoformat(), "timeZone": tz_name},
        "extendedProperties": {
            "private": {MARKER_KEY: MARKER_VALUE, SOURCE_KEY: gmail_message_id}
        },
    }
    try:
        event = svc.events().insert(calendarId=_CALENDAR_ID, body=body).execute()
    except Exception as e:
        _record_terminal(
            conn,
            action_id,
            "calendar_create",
            actor,
            run_id,
            gmail_message_id,
            detail,
            "failed",
            str(e),
        )
        raise

    _record_terminal(
        conn,
        action_id,
        "calendar_create",
        actor,
        run_id,
        gmail_message_id,
        f"{detail}; event_id={event['id']}",
        "confirmed",
        None,
    )
    return EventResult(event["id"], start_local.isoformat(), end_local.isoformat())


def move_event(
    conn: sqlite3.Connection,
    svc: Resource,
    run_id: str,
    event_id: str,
    start: str,
    *,
    tz_name: str = DEFAULT_TIMEZONE,
    actor: str = "agent",
) -> EventResult:
    """Move an agent-created event to a new start, preserving its duration.
    Raises ValueError (no API call, no audit row) if the event isn't
    marker-tagged or can't be read."""
    event = _get_own_event(svc, event_id)
    old_start = datetime.fromisoformat(event["start"]["dateTime"])
    old_end = datetime.fromisoformat(event["end"]["dateTime"])
    duration = old_end - old_start

    new_start_local = _parse_local(start)
    new_end_local = new_start_local + duration

    action_id = store.new_id()
    detail = f"event_id={event_id}; start={new_start_local.isoformat()}"
    _record_intended(conn, action_id, "calendar_move", actor, run_id, None, detail)

    body = {
        "start": {"dateTime": new_start_local.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": new_end_local.isoformat(), "timeZone": tz_name},
    }
    try:
        svc.events().patch(
            calendarId=_CALENDAR_ID, eventId=event_id, body=body
        ).execute()
    except Exception as e:
        _record_terminal(
            conn,
            action_id,
            "calendar_move",
            actor,
            run_id,
            None,
            detail,
            "failed",
            str(e),
        )
        raise

    _record_terminal(
        conn,
        action_id,
        "calendar_move",
        actor,
        run_id,
        None,
        detail,
        "confirmed",
        None,
    )
    return EventResult(event_id, new_start_local.isoformat(), new_end_local.isoformat())


def delete_event(
    conn: sqlite3.Connection,
    svc: Resource,
    run_id: str,
    event_id: str,
    *,
    actor: str = "agent",
) -> None:
    """Delete an agent-created event. Raises ValueError (no API call, no audit
    row) if the event isn't marker-tagged or can't be read."""
    _get_own_event(svc, event_id)

    action_id = store.new_id()
    detail = f"event_id={event_id}"
    _record_intended(conn, action_id, "calendar_delete", actor, run_id, None, detail)

    try:
        svc.events().delete(calendarId=_CALENDAR_ID, eventId=event_id).execute()
    except Exception as e:
        _record_terminal(
            conn,
            action_id,
            "calendar_delete",
            actor,
            run_id,
            None,
            detail,
            "failed",
            str(e),
        )
        raise

    _record_terminal(
        conn,
        action_id,
        "calendar_delete",
        actor,
        run_id,
        None,
        detail,
        "confirmed",
        None,
    )


def _get_own_event(svc: Resource, event_id: str) -> dict:
    """Read an event and enforce the write boundary: refuse (ValueError) unless
    it carries this assistant's ownership marker. The only read call move/delete
    make before deciding whether to mutate anything."""
    try:
        event = svc.events().get(calendarId=_CALENDAR_ID, eventId=event_id).execute()
    except Exception as e:
        raise ValueError(f"could not read event {event_id}: {e}") from e

    props = (event.get("extendedProperties") or {}).get("private") or {}
    if props.get(MARKER_KEY) != MARKER_VALUE:
        raise ValueError(
            f"refusing to modify event {event_id}: not created by this assistant "
            "(missing ownership marker)"
        )
    return event


def _parse_local(iso: str) -> datetime:
    """Parse a naive ISO timestamp as local wall-clock. Rejects an offset-aware
    input loudly — silently assuming UTC would book the wrong wall-clock time."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        raise ValueError(f"expected a naive local timestamp, got offset-aware: {iso!r}")
    return dt


def _record_intended(
    conn: sqlite3.Connection,
    action_id: str,
    action_type: str,
    actor: str,
    run_id: str,
    gmail_message_id: str | None,
    detail: str,
) -> None:
    conn.execute(
        "INSERT INTO action_events"
        "(action_id, status, action_type, actor, run_id, gmail_message_id, "
        " detail, recorded_at) VALUES (?, 'intended', ?, ?, ?, ?, ?, ?)",
        (
            action_id,
            action_type,
            actor,
            run_id,
            gmail_message_id,
            detail,
            store.now_iso(),
        ),
    )
    conn.commit()


def _record_terminal(
    conn: sqlite3.Connection,
    action_id: str,
    action_type: str,
    actor: str,
    run_id: str,
    gmail_message_id: str | None,
    detail: str,
    status: str,
    error: str | None,
) -> None:
    conn.execute(
        "INSERT INTO action_events"
        "(action_id, status, action_type, actor, run_id, gmail_message_id, "
        " detail, error, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            action_id,
            status,
            action_type,
            actor,
            run_id,
            gmail_message_id,
            detail,
            error,
            store.now_iso(),
        ),
    )
    conn.commit()
