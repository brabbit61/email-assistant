"""Telegram sendMessage: the worker's direct, agent-independent P1 ping path
(T2.2, issue #42; formats/triggers signed off in S2.2, #38).

Two responsibilities: the raw stdlib POST to Telegram's API (`send`), and the
P1 ping orchestration (`notify_p1`) — dedupe, burst-collapse into one message,
and audit-before-write, mirroring apply.py's intended/confirmed pattern (one
`action_events` row per message, written before the call; terminal status
after). Unlike a label add, a Telegram send isn't idempotent, so a crash
between `intended` and `confirmed` risks a rare duplicate ping on retry —
accepted (loud beats silent for a P1).

Strictly send-only: never calls getUpdates or registers a webhook, so it never
contends with the hermes gateway's long-poll (T2.1, #41 — the gateway is the
bot's sole poller; sendMessage has no such single-consumer limit).

A failed send is recorded as a `failed` action_event and swallowed here — it
never raises past `notify_p1`, never touches `run_events`, and never affects
the run's exit code. Notification health and triage health are separate
signals (T2.2 decision): a flaky Telegram API must not make `assistant
status` call a healthy worker unhealthy, and must not feed S2.2's N=5
consecutive-run-failure alert, which is reserved for actual triage failures.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from typing import Callable

from assistant import store
from assistant.classify import Verdict

_API = "https://api.telegram.org/bot{token}/sendMessage"

SendFn = Callable[[str, str, str], None]


def send(token: str, chat_id: str, text: str, *, timeout: float = 10.0) -> None:
    """POST one message. Raises on a non-2xx response or any network/timeout error."""
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        _API.format(token=token),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Telegram API returned {resp.status}")


def _already_notified(conn: sqlite3.Connection, gmail_message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM action_events WHERE gmail_message_id = ? "
        "AND action_type = 'telegram_ping' AND status = 'confirmed' LIMIT 1",
        (gmail_message_id,),
    ).fetchone()
    return row is not None


def _compose(fresh: list[tuple[str, sqlite3.Row, Verdict]]) -> str:
    """P1 mockups per #38, minus the two Phase-2 gaps: the draft-link line
    (Phase 2 has no drafting — S2.3's grounding rule omits rather than
    fabricates) and the burst's "reply with a number to open" line (the
    worker never receives replies — that's the hermes gateway, #41 — and no
    Phase-2 intent resolves a bare numeric reference; see #42 decisions)."""
    if len(fresh) == 1:
        _, row, verdict = fresh[0]
        return f"🔴 Urgent — {row['sender']} — {row['subject']} \n{verdict.reasoning}"
    lines = [f"🔴 {len(fresh)} urgent — caught up"]
    lines += [f"• {row['sender']} · {verdict.reasoning}" for _, row, verdict in fresh]
    return "\n".join(lines)


def _record(
    conn: sqlite3.Connection,
    actions: list[tuple[str, str]],
    run_id: str,
    status: str,
    error: str | None,
) -> None:
    now = store.now_iso()
    for action_id, gmail_message_id in actions:
        conn.execute(
            "INSERT INTO action_events"
            "(action_id, status, action_type, actor, run_id, gmail_message_id, "
            " error, recorded_at) VALUES (?, ?, 'telegram_ping', 'worker', ?, ?, ?, ?)",
            (action_id, status, run_id, gmail_message_id, error, now),
        )
    conn.commit()


def notify_p1(
    conn: sqlite3.Connection,
    run_id: str,
    token: str,
    chat_id: str,
    hits: list[tuple[str, sqlite3.Row, Verdict]],
    *,
    send_fn: SendFn | None = None,
) -> None:
    """Ping once for every newly-classified P1 this run — deduped per
    `gmail_message_id` (once ever) and collapsed into one combined message on
    a burst. `hits` is (gmail_message_id, message_row, verdict) tuples, already
    filtered to this run's successfully-applied P1-Urgent classifications.
    Never raises: see module docstring on failure isolation.

    `send_fn` defaults to this module's own `send`, looked up at call time (not
    bound as a default-argument value) so `monkeypatch.setattr(telegram, "send",
    ...)` reaches callers — like cli.py — that don't pass `send_fn` explicitly."""
    fresh = [h for h in hits if not _already_notified(conn, h[0])]
    if not fresh:
        return

    actions = [(store.new_id(), gmail_message_id) for gmail_message_id, _, _ in fresh]
    _record(conn, actions, run_id, "intended", None)

    do_send = send_fn or send
    try:
        do_send(token, chat_id, _compose(fresh))
    except Exception as e:
        _record(conn, actions, run_id, "failed", str(e))
        return

    _record(conn, actions, run_id, "confirmed", None)


# --- operational pings (T2.3, #43) -------------------------------------------
# Budget-breach, consecutive-failure, OAuth-death and recovery alerts. Formats
# and thresholds are signed off in #38; this is only the send/dedupe wiring.
# Same contract as notify_p1: one action_events row per ping (here with no
# gmail_message_id), audit-before-write, and never raises. Unlike P1 these fire
# regardless of dry_run — they report real worker state (spend, crashes, dead
# auth), which is just as real during the dry-run trial (#43 decision).

FAILURE_THRESHOLD = 5  # consecutive crashed runs before the failure alert (#38)


def _record_op(
    conn: sqlite3.Connection,
    action_id: str,
    run_id: str,
    action_type: str,
    status: str,
    detail: str | None,
    error: str | None,
) -> None:
    conn.execute(
        "INSERT INTO action_events"
        "(action_id, status, action_type, actor, run_id, detail, error, recorded_at)"
        " VALUES (?, ?, ?, 'worker', ?, ?, ?, ?)",
        (action_id, status, action_type, run_id, detail, error, store.now_iso()),
    )
    conn.commit()


def _send_op(
    conn: sqlite3.Connection,
    run_id: str,
    token: str,
    chat_id: str,
    action_type: str,
    text: str,
    detail: str | None,
    send_fn: SendFn | None,
) -> None:
    """intended -> send -> confirmed/failed for one operational ping. Never raises
    (same isolation as notify_p1: a flaky Telegram must not break the run)."""
    action_id = store.new_id()
    _record_op(conn, action_id, run_id, action_type, "intended", detail, None)
    do_send = send_fn or send
    try:
        do_send(token, chat_id, text)
    except Exception as e:
        _record_op(conn, action_id, run_id, action_type, "failed", detail, str(e))
        return
    _record_op(conn, action_id, run_id, action_type, "confirmed", detail, None)


def _last_triage_success(conn: sqlite3.Connection) -> str | None:
    """recorded_at of the last completed run (ok or per-message-error), or None.
    A crashed run writes status='failed', so it's excluded — that's the boundary
    the failure streak and recovery both measure from."""
    return conn.execute(
        "SELECT MAX(recorded_at) FROM run_events "
        "WHERE phase='triage' AND status IN ('ok','error')"
    ).fetchone()[0]


def notify_failure(
    conn: sqlite3.Connection,
    run_id: str,
    token: str,
    chat_id: str,
    last_error: str,
    *,
    send_fn: SendFn | None = None,
) -> None:
    """Fire once when consecutive crashed runs reach FAILURE_THRESHOLD (#38).
    Call from the run's crash path *after* its status='failed' triage row is
    written. The streak = 'failed' triage rows since the last ok/error; dedupe =
    at most one failure_ping per streak (none newer than that last success)."""
    streak = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE phase='triage' AND status='failed' "
        "AND event_id > COALESCE("
        "  (SELECT MAX(event_id) FROM run_events WHERE phase='triage' "
        "   AND status IN ('ok','error')), 0)"
    ).fetchone()[0]
    if streak < FAILURE_THRESHOLD:
        return
    last_ok = _last_triage_success(conn)
    already = conn.execute(
        "SELECT 1 FROM action_events WHERE action_type='failure_ping' "
        "AND status='confirmed' AND recorded_at > ? LIMIT 1",
        (last_ok or "",),
    ).fetchone()
    if already:
        return
    success = f"{store.clock(last_ok)} ({store.age(last_ok)})" if last_ok else "never"
    text = (
        f"⚠️ Worker failing — {streak} runs in a row\n"
        f"Last error: {last_error}\n"
        f"Last success: {success}\n"
        "`assistant status` for detail."
    )
    _send_op(
        conn,
        run_id,
        token,
        chat_id,
        "failure_ping",
        text,
        f"{streak} consecutive",
        send_fn,
    )


def notify_recovery(
    conn: sqlite3.Connection,
    run_id: str,
    token: str,
    chat_id: str,
    *,
    send_fn: SendFn | None = None,
) -> None:
    """On a clean completion, fire once if an unacknowledged alert is outstanding
    — the latest failure_ping or oauth_ping is newer than the latest recovery_ping.
    One recovery message covers both a failure streak and an OAuth outage (#43)."""
    last_alert, last_recovery = conn.execute(
        "SELECT "
        "(SELECT MAX(recorded_at) FROM action_events "
        " WHERE action_type IN ('failure_ping','oauth_ping') AND status='confirmed'), "
        "(SELECT MAX(recorded_at) FROM action_events "
        " WHERE action_type='recovery_ping' AND status='confirmed')"
    ).fetchone()
    if last_alert is None or (
        last_recovery is not None and last_alert <= last_recovery
    ):
        return
    text = f"✅ Worker recovered — back to normal at {store.clock(store.now_iso())}."
    _send_op(conn, run_id, token, chat_id, "recovery_ping", text, None, send_fn)


def notify_budget(
    conn: sqlite3.Connection,
    run_id: str,
    token: str,
    chat_id: str,
    *,
    soft_cap: float,
    monthly_cap: float,
    send_fn: SendFn | None = None,
) -> None:
    """Fire once on the first run of a UTC day whose cumulative spend crosses the
    daily soft cap (#38). UTC not local (#43) — matches the cost ledger and what
    `assistant costs` shows. Ping-only; nothing pauses."""
    today = store.now_iso()[:10]
    spend = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE created_at LIKE ?",
        (f"{today}%",),
    ).fetchone()[0]
    if spend <= soft_cap:
        return
    already = conn.execute(
        "SELECT 1 FROM action_events WHERE action_type='budget_ping' "
        "AND status='confirmed' AND recorded_at LIKE ? LIMIT 1",
        (f"{today}%",),
    ).fetchone()
    if already:
        return
    text = (
        "💸 Daily budget — soft cap passed\n"
        f"Today: ${spend:.2f} / ${soft_cap:.2f} soft cap\n"
        f"Nothing paused; ${monthly_cap:.0f}/mo console hard cap still guards.\n"
        "`assistant costs` for the breakdown."
    )
    _send_op(
        conn,
        run_id,
        token,
        chat_id,
        "budget_ping",
        text,
        f"${spend:.2f}/${soft_cap:.2f}",
        send_fn,
    )


def notify_oauth_death(
    conn: sqlite3.Connection,
    run_id: str,
    token: str,
    chat_id: str,
    error: str,
    *,
    send_fn: SendFn | None = None,
) -> None:
    """Fire immediately on permanent auth failure (gmail.AuthError), once per
    outage. get_credentials runs before any run_event, so an auth death writes
    none and never feeds the failure counter (#38); dedupe instead keys on the
    last successful auth (the last triage 'started' row) — silent until a later
    run authenticates again."""
    last_auth = conn.execute(
        "SELECT MAX(recorded_at) FROM run_events "
        "WHERE phase='triage' AND status='started'"
    ).fetchone()[0]
    already = conn.execute(
        "SELECT 1 FROM action_events WHERE action_type='oauth_ping' "
        "AND status='confirmed' AND recorded_at > ? LIMIT 1",
        (last_auth or "",),
    ).fetchone()
    if already:
        return
    last_ok = _last_triage_success(conn)
    good = store.clock(last_ok) if last_ok else "never"
    text = (
        "🔑 Gmail auth dead — worker stopped\n"
        "Token refresh failed permanently; every run fails until you re-auth.\n"
        "Fix: delete secrets/token.json, then `uv run python -m assistant.gmail`\n"
        f"Since {store.clock(store.now_iso())} · last good run {good}"
    )
    _send_op(conn, run_id, token, chat_id, "oauth_ping", text, error[:120], send_fn)
