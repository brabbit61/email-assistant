"""Human corrections to classifications — the two capture channels of the
improvement loop, both landing an append-only `classifications` row
marked with its `source`.

- **Flow B — `correct()`** (chat): the agent runs `assistant correct <id>
  --category X [--priority Y]`. We fix the Gmail label (remove the superseded
  taxonomy label, add the new one) *first*, then record the re-classification —
  Gmail-first so a failed relabel never leaves a row claiming a change that
  didn't happen. `source='human-chat'`.
- **Flow A — `detect_relabels()`** (poll): the user relabels a triaged message in
  Gmail; the next poll sees the taxonomy labels no longer match the latest verdict
  and records the human re-classification. `source='human-gmail'`. Gmail is already
  right, so nothing is written back to Gmail.

Human rows carry `llm_call_id = NULL` (no model call) and a short provenance
`reasoning`. The worker never re-classifies a human row — see the retry filter in
`cli._messages_needing_classification`.
"""

from __future__ import annotations

import sqlite3

from googleapiclient.discovery import Resource

from assistant import apply, gmail, labels, store
from assistant.classify import UNCLASSIFIED

# taxonomy full label name -> key, e.g. "👤 Personal" -> "Personal".
_KEY_BY_NAME = {name: key for key, name in labels.FULL_NAME.items()}
_CATEGORY_NAMES = {labels.FULL_NAME[c] for c in labels.CATEGORIES}
_PRIORITY_NAMES = {labels.FULL_NAME[p] for p in labels.PRIORITIES}


def record_human(
    conn: sqlite3.Connection,
    gmail_message_id: str,
    category: str,
    priority: str | None,
    reasoning: str,
    source: str,
) -> None:
    """Append a human-originated classification row (supersedes the prior verdict).
    `llm_call_id` NULL; `source` in ('human-chat','human-gmail')."""
    conn.execute(
        "INSERT INTO classifications"
        "(gmail_message_id, category, priority, reasoning, llm_call_id, "
        " classified_at, source) VALUES (?,?,?,?,NULL,?,?)",
        (gmail_message_id, category, priority, reasoning, store.now_iso(), source),
    )
    conn.commit()


def correct(
    conn: sqlite3.Connection,
    svc: Resource,
    gmail_message_id: str,
    category: str,
    priority: str | None,
) -> str:
    """Flow B: apply a chat correction to Gmail + the DB, return a summary line.

    An omitted `priority` clears any priority label the message carries. Raises
    ValueError if the message id is unknown (never seen by the poller)."""
    known = conn.execute(
        "SELECT 1 FROM messages WHERE gmail_message_id = ?", (gmail_message_id,)
    ).fetchone()
    if known is None:
        raise ValueError(f"unknown message id: {gmail_message_id}")

    cur_cat, cur_pri = _current_verdict(conn, gmail_message_id)

    target = {labels.FULL_NAME[category]}
    if priority:
        target.add(labels.FULL_NAME[priority])

    name_to_id = labels.label_ids(svc)  # full_name -> id, taxonomy only
    id_to_name = {i: name for name, i in name_to_id.items()}
    current = {
        id_to_name[i]
        for i in gmail.message_labels(svc, gmail_message_id)
        if i in id_to_name
    }
    add_names = sorted(target - current)
    remove_names = sorted(current - target)

    # Gmail first: a failed relabel raises before any classification row is written.
    if add_names or remove_names:
        apply.apply_relabel(
            conn,
            svc,
            store.new_id(),
            gmail_message_id,
            add_names=add_names,
            remove_names=remove_names,
            actor="human-chat",
        )

    verdict_changed = (category, priority) != (cur_cat, cur_pri)
    if verdict_changed:
        record_human(
            conn,
            gmail_message_id,
            category,
            priority,
            f"corrected via chat (was {_describe(cur_cat, cur_pri)})",
            "human-chat",
        )

    if not add_names and not remove_names and not verdict_changed:
        return f"Already {_describe(category, priority)} — no change."
    parts = []
    if remove_names:
        parts.append("removed " + ", ".join(remove_names))
    if add_names:
        parts.append("added " + ", ".join(add_names))
    detail = "; ".join(parts) if parts else "Gmail already labelled"
    return (
        f"Corrected {gmail_message_id}: {_describe(cur_cat, cur_pri)} -> "
        f"{_describe(category, priority)} ({detail})."
    )


def detect_relabels(
    conn: sqlite3.Connection,
    svc: Resource,
    label_events: list[tuple[str, frozenset[str]]],
) -> int:
    """Flow A: record a human re-classification for each already-triaged message
    whose Gmail taxonomy labels drifted from its latest verdict. Returns the count.

    The worker's own `apply_verdict` and `correct()`'s modifies also emit label
    events, but they leave the labels equal to the latest verdict, so the
    observed==expected check filters them out — no self-detection, and a replayed
    history window (crash reprocess) is idempotent for the same reason."""
    if not label_events:
        return 0

    name_to_id = labels.label_ids(svc)  # full_name -> id, taxonomy only
    taxonomy_ids = set(name_to_id.values())
    id_to_name = {i: name for name, i in name_to_id.items()}

    candidates = {mid for mid, changed in label_events if changed & taxonomy_ids}
    recorded = 0
    for mid in candidates:
        cur_cat, cur_pri = _current_verdict(conn, mid)
        if cur_cat is None:
            continue  # never classified — a relabel here isn't a correction

        # Authoritative current taxonomy labels (the event snapshot may be stale).
        observed = {
            id_to_name[i] for i in gmail.message_labels(svc, mid) if i in id_to_name
        }
        expected = set()
        if cur_cat != UNCLASSIFIED:
            expected.add(labels.FULL_NAME[cur_cat])
        if cur_pri:
            expected.add(labels.FULL_NAME[cur_pri])
        if observed == expected:
            continue  # matches the latest verdict → worker/correct self-op, not drift

        observed_categories = observed & _CATEGORY_NAMES
        if len(observed_categories) > 1:
            continue  # stacked categories: ambiguous, never guess

        if not observed_categories:
            new_cat, new_pri = UNCLASSIFIED, None
            reasoning = "taxonomy label removed in Gmail"
        else:
            new_cat = _KEY_BY_NAME[next(iter(observed_categories))]
            observed_priorities = observed & _PRIORITY_NAMES
            new_pri = (
                _KEY_BY_NAME[next(iter(observed_priorities))]
                if len(observed_priorities) == 1
                else None
            )
            reasoning = f"relabeled in Gmail (was {_describe(cur_cat, cur_pri)})"

        record_human(conn, mid, new_cat, new_pri, reasoning, "human-gmail")
        recorded += 1
    return recorded


def _current_verdict(
    conn: sqlite3.Connection, gmail_message_id: str
) -> tuple[str | None, str | None]:
    """(category, priority) of the message's latest classification, or (None, None)
    if it was never classified."""
    row = conn.execute(
        "SELECT category, priority FROM current_classifications "
        "WHERE gmail_message_id = ?",
        (gmail_message_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row["category"], row["priority"]


def _describe(category: str | None, priority: str | None) -> str:
    if category is None:
        return "unclassified"
    return f"{category}/{priority}" if priority else category
