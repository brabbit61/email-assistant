"""Draft-reply: compose + place a Gmail draft.

The agent composes a reply, writes it to a file, and `create_draft_reply` does
the Gmail-side placement: reply-all (`To` = the latest inbound message's
sender, `Cc` = that message's To+Cc minus the caller's own address — native
Gmail Reply-All semantics, derived from the *latest* message only, not the
whole thread), proper `In-Reply-To`/`References` +
`threadId` threading, and a Gmail-style quote-back appended below the agent's
text. Same audit-before-write contract as `apply.py` (`intended` row before
the `drafts.create` call, `confirmed`/`failed` after); `action_type='draft'`.
No `dry_run` — like `apply_relabel`, this is an explicit, user-initiated action.

Resolution is DB-only: the reply target is the latest `messages` row in the
thread whose sender isn't the caller, and every header needed (Message-ID,
References, To, Cc) is parsed from that row's stored `raw_json`. No live
Gmail fetch.

ponytail: DB-only resolution means a message that arrived since the last poll
(up to ~5 min) won't be seen — bounded staleness, not a live thread fetch.
Add a live threads().get() fallback if that ceiling ever bites.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr

from googleapiclient.discovery import Resource

from assistant import store


@dataclass
class DraftResult:
    draft_id: str
    to: str
    cc: list[str]


def create_draft_reply(
    conn: sqlite3.Connection,
    svc: Resource,
    thread_id: str,
    body_text: str,
    *,
    actor: str = "agent",
) -> DraftResult:
    """Place a reply-all draft in the given thread. Raises ValueError for any
    pre-flight problem (empty body, unknown thread, no inbound message) before
    any Gmail call or action_events write."""
    if not body_text or not body_text.strip():
        raise ValueError("draft body is empty")

    my_email = svc.users().getProfile(userId="me").execute()["emailAddress"]
    row = _latest_inbound(conn, thread_id, my_email)
    headers = _headers(row)
    to_addr, cc_addrs = _recipients(row, headers, my_email)

    mime = EmailMessage()
    mime["From"] = my_email
    mime["To"] = to_addr
    if cc_addrs:
        mime["Cc"] = ", ".join(cc_addrs)
    subject = _reply_subject(row["subject"])
    if subject:
        mime["Subject"] = subject
    message_id, references = _threading_headers(headers)
    if message_id:
        mime["In-Reply-To"] = message_id
        mime["References"] = references
    mime.set_content(_compose_body(body_text, row, headers))
    raw = base64.urlsafe_b64encode(bytes(mime)).decode("ascii")

    gmail_message_id = row["gmail_message_id"]
    action_id = store.new_id()
    run_id = store.new_id()
    detail = f"to={to_addr}; cc={', '.join(cc_addrs) if cc_addrs else '(none)'}"

    conn.execute(
        "INSERT INTO action_events"
        "(action_id, status, action_type, actor, run_id, gmail_message_id, "
        " thread_id, detail, recorded_at) "
        "VALUES (?, 'intended', 'draft', ?, ?, ?, ?, ?, ?)",
        (
            action_id,
            actor,
            run_id,
            gmail_message_id,
            thread_id,
            detail,
            store.now_iso(),
        ),
    )
    conn.commit()

    try:
        draft = (
            svc.users()
            .drafts()
            .create(
                userId="me",
                body={"message": {"raw": raw, "threadId": thread_id}},
            )
            .execute()
        )
    except Exception as e:
        _record_terminal(
            conn,
            action_id,
            actor,
            run_id,
            gmail_message_id,
            thread_id,
            detail,
            "failed",
            str(e),
        )
        raise  # no orphaned draft: drafts.create is atomic, nothing partial to clean up

    _record_terminal(
        conn,
        action_id,
        actor,
        run_id,
        gmail_message_id,
        thread_id,
        detail,
        "confirmed",
        None,
    )
    return DraftResult(draft["id"], to_addr, cc_addrs)


def _latest_inbound(
    conn: sqlite3.Connection, thread_id: str, my_email: str
) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT gmail_message_id, sender, subject, body, internal_date_ms, raw_json "
        "FROM messages WHERE thread_id = ? ORDER BY internal_date_ms DESC",
        (thread_id,),
    ).fetchall()
    if not rows:
        raise ValueError(f"no messages found for thread: {thread_id}")

    my = my_email.lower()
    for row in rows:
        if not row["sender"]:
            continue
        _, addr = parseaddr(row["sender"])
        if addr.lower() != my:
            return row
    raise ValueError(f"no inbound message found in thread: {thread_id}")


def _headers(row: sqlite3.Row) -> dict[str, str]:
    raw = json.loads(row["raw_json"] or "{}")
    return {
        h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])
    }


def _recipients(
    row: sqlite3.Row, headers: dict[str, str], my_email: str
) -> tuple[str, list[str]]:
    """Native Gmail Reply-All: To = original sender; Cc = original's To+Cc,
    minus self and the primary recipient, deduped."""
    _, to_email = parseaddr(row["sender"])
    seen = {my_email.lower(), to_email.lower()}
    cc: list[str] = []
    for name, addr in getaddresses([headers.get("to", ""), headers.get("cc", "")]):
        key = addr.lower()
        if not addr or key in seen:
            continue
        seen.add(key)
        cc.append(formataddr((name, addr)))
    return row["sender"], cc


def _threading_headers(headers: dict[str, str]) -> tuple[str | None, str | None]:
    message_id = headers.get("message-id")
    if not message_id:
        return None, None  # thread still nests via threadId alone
    existing_refs = headers.get("references", "")
    references = (
        f"{existing_refs} {message_id}".strip() if existing_refs else message_id
    )
    return message_id, references


def _reply_subject(subject: str | None) -> str:
    subject = (subject or "").strip()
    if not subject:
        return ""
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _compose_body(agent_text: str, row: sqlite3.Row, headers: dict[str, str]) -> str:
    ms = row["internal_date_ms"]
    if ms:
        date_str = datetime.fromtimestamp(ms / 1000).strftime("%b %-d, %Y, %-I:%M %p")
        attribution = f"On {date_str}, {row['sender']} wrote:"
    else:
        attribution = f"{row['sender']} wrote:"  # ponytail: raw Date header not parsed
    quoted = "\n".join(f"> {line}" for line in (row["body"] or "").splitlines())
    return f"{agent_text.rstrip()}\n\n{attribution}\n{quoted}\n"


def _record_terminal(
    conn: sqlite3.Connection,
    action_id: str,
    actor: str,
    run_id: str,
    gmail_message_id: str,
    thread_id: str,
    detail: str,
    status: str,
    error: str | None,
) -> None:
    conn.execute(
        "INSERT INTO action_events"
        "(action_id, status, action_type, actor, run_id, gmail_message_id, "
        " thread_id, detail, error, recorded_at) "
        "VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)",
        (
            action_id,
            status,
            actor,
            run_id,
            gmail_message_id,
            thread_id,
            detail,
            error,
            store.now_iso(),
        ),
    )
    conn.commit()
