"""Label applier: audit-before-write application of classifier verdicts to Gmail
(T1.7, issue #13).

`intended` `action_events` rows are written before the Gmail API call; `confirmed`
(success) or `failed` (re-raised) rows are written after — the audit trail can
never claim less than what actually happened. One `action_id` per elementary
mutation (each label, plus archive when enabled), but exactly one combined
`messages.modify` call per message: every `intended` event precedes it, every
terminal event follows it.

Idempotent: applying the same labels twice is harmless (Gmail's `addLabelIds` is
a no-op on a label already present), so a crash-reprocessed message just gets a
new `action_id` — an extra log entry, not a bug (T1.5's crash-reprocess safety
depends on this).

`dry_run=True` skips the Gmail call *and* every `action_events` write — an
`intended`-only row with no eventual terminal event would look identical to a
genuine mid-crash, and a dry run isn't one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from googleapiclient.discovery import Resource

from assistant import store
from assistant.classify import UNCLASSIFIED, Verdict
from assistant.labels import FULL_NAME, label_ids


@dataclass
class ApplyResult:
    labels: list[str]  # full label names applied (or would be, if dry_run)
    archived: bool
    action_ids: list[str]  # empty when dry_run, or when the verdict was UNCLASSIFIED
    dry_run: bool


def apply_verdict(
    conn: sqlite3.Connection,
    svc: Resource,
    run_id: str,
    gmail_message_id: str,
    verdict: Verdict,
    *,
    actor: str = "worker",
    auto_archive: bool = False,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply one classification's labels (+archive, if enabled) to Gmail. Never
    called for an UNCLASSIFIED verdict by the run loop, but safe (no-op) if it is."""
    if verdict.category == UNCLASSIFIED:
        return ApplyResult(labels=[], archived=False, action_ids=[], dry_run=dry_run)

    target_names = [FULL_NAME[verdict.category]]
    if verdict.priority:
        target_names.append(FULL_NAME[verdict.priority])
    will_archive = auto_archive and verdict.category == "Low-Value"

    if dry_run:
        return ApplyResult(target_names, will_archive, [], dry_run=True)

    # Resolve ids before writing any intended row: a missing label (taxonomy not
    # reconciled) is a setup problem, not a per-message crash — it shouldn't leave
    # a dangling unconfirmed action behind.
    ids = label_ids(svc)
    add_ids = [ids[name] for name in target_names]

    actions = [(store.new_id(), "label_add", name) for name in target_names]
    if will_archive:
        actions.append((store.new_id(), "archive", "INBOX"))

    now = store.now_iso()
    for action_id, action_type, detail in actions:
        conn.execute(
            "INSERT INTO action_events"
            "(action_id, status, action_type, actor, run_id, gmail_message_id, "
            " detail, recorded_at) VALUES (?, 'intended', ?, ?, ?, ?, ?, ?)",
            (action_id, action_type, actor, run_id, gmail_message_id, detail, now),
        )
    conn.commit()

    body: dict = {"addLabelIds": add_ids}
    if will_archive:
        body["removeLabelIds"] = ["INBOX"]

    try:
        svc.users().messages().modify(
            userId="me", id=gmail_message_id, body=body
        ).execute()
    except Exception as e:
        _record_terminal(
            conn, actions, actor, run_id, gmail_message_id, "failed", str(e)
        )
        raise  # per-message isolation is the run loop's job, not ours

    _record_terminal(conn, actions, actor, run_id, gmail_message_id, "confirmed", None)
    return ApplyResult(target_names, will_archive, [a[0] for a in actions], False)


def apply_relabel(
    conn: sqlite3.Connection,
    svc: Resource,
    run_id: str,
    gmail_message_id: str,
    *,
    add_names: list[str],
    remove_names: list[str],
    actor: str,
) -> None:
    """Apply a human correction to Gmail: add the new taxonomy labels, remove the
    superseded ones, in one combined modify. Same audit-before-write contract as
    `apply_verdict` (one `action_id` per elementary add/remove; `label_remove` is a
    new action_type). No dry_run — a correction is an explicit, Jenit-initiated
    action, not the unattended run the go-live gate protects."""
    actions = [(store.new_id(), "label_add", name) for name in add_names]
    actions += [(store.new_id(), "label_remove", name) for name in remove_names]
    if not actions:
        return

    ids = label_ids(svc)
    add_ids = [ids[name] for name in add_names]
    remove_ids = [ids[name] for name in remove_names]

    now = store.now_iso()
    for action_id, action_type, detail in actions:
        conn.execute(
            "INSERT INTO action_events"
            "(action_id, status, action_type, actor, run_id, gmail_message_id, "
            " detail, recorded_at) VALUES (?, 'intended', ?, ?, ?, ?, ?, ?)",
            (action_id, action_type, actor, run_id, gmail_message_id, detail, now),
        )
    conn.commit()

    body: dict = {}
    if add_ids:
        body["addLabelIds"] = add_ids
    if remove_ids:
        body["removeLabelIds"] = remove_ids

    try:
        svc.users().messages().modify(
            userId="me", id=gmail_message_id, body=body
        ).execute()
    except Exception as e:
        _record_terminal(
            conn, actions, actor, run_id, gmail_message_id, "failed", str(e)
        )
        raise

    _record_terminal(conn, actions, actor, run_id, gmail_message_id, "confirmed", None)


def _record_terminal(
    conn: sqlite3.Connection,
    actions: list[tuple[str, str, str]],
    actor: str,
    run_id: str,
    gmail_message_id: str,
    status: str,
    error: str | None,
) -> None:
    now = store.now_iso()
    for action_id, action_type, detail in actions:
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
                now,
            ),
        )
    conn.commit()
