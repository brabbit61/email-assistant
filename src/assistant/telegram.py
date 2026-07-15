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
        return f'🔴 Urgent — {row["sender"]}\n{verdict.reasoning}\n"{row["subject"]}"'
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
