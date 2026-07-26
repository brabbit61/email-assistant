"""Incremental Gmail poller.

Discovers newly-arrived INBOX mail and durably records a complete `messages` row
for each — metadata + decoded body. No LLM, no labels: classification is.

State lives entirely in SQLite. Each run reads the last checkpoint (the
`current_checkpoint` view over append-only `run_events`), asks Gmail what's new,
ingests it, then advances the checkpoint — **in that order**, so a crash never
loses mail: the next run re-lists the same window and the message PK dedups.

Three regimes:
- **Cold start** (no checkpoint): bootstrap forward from the mailbox's current
  historyId. Pre-existing mail is Phase-3 backfill's job, not the poller's.
- **Normal wake**: history.list since the checkpoint, draining all pages.
- **Long outage** (checkpoint historyId expired → 404): a bounded messages.list
  sweep from the last checkpoint time, recorded as a loud catch-up gap event.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

from assistant import correct, gmail, store


@dataclass
class RunResult:
    messages_seen: int
    inserted: int
    catchup: bool
    history_id: str


def _epoch_s(iso: str) -> int:
    """Parse the schema's timestamp text (`now_iso()`) to Unix seconds (UTC)."""
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def poll_once(conn: sqlite3.Connection, svc: Resource) -> RunResult:
    """One poll pass: discover new INBOX mail, record it, advance the checkpoint."""
    run_id = store.new_id()
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, recorded_at) "
        "VALUES (?, 'poll', 'started', ?)",
        (run_id, store.now_iso()),
    )
    conn.commit()

    ckpt = conn.execute(
        "SELECT history_id, recorded_at FROM current_checkpoint"
    ).fetchone()

    catchup = False
    # (message_id, changed label ids) events for Flow-A relabel detection.
    # The cold-start and catch-up regimes carry none (getProfile / messages.list
    # return no label history).
    label_events: list[tuple[str, frozenset[str]]] = []
    if ckpt is None:
        # Cold start: forward-only. Record the baseline historyId, ingest nothing.
        ids: list[str] = []
        new_history_id = gmail.current_history_id(svc)
    else:
        try:
            ids, label_events, new_history_id = gmail.iter_history(
                svc, ckpt["history_id"]
            )
        except HttpError as e:
            if e.resp.status != 404:
                raise  # transient/other: fail loudly, next timer tick retries
            # historyId aged out of Gmail's ~1-week window: bounded catch-up sweep.
            catchup = True
            since = _epoch_s(ckpt["recorded_at"])
            conn.execute(
                "INSERT INTO run_events"
                "(run_id, phase, status, history_id, note, recorded_at) "
                "VALUES (?, 'catchup', 'gap', ?, ?, ?)",
                (
                    run_id,
                    ckpt["history_id"],
                    f"historyId expired; messages.list sweep after {since}",
                    store.now_iso(),
                ),
            )
            conn.commit()
            ids = gmail.list_message_ids(svc, f"in:inbox after:{since}")
            new_history_id = gmail.current_history_id(svc)

    inserted = 0
    for msg_id in ids:
        row = gmail.get_message(svc, msg_id)
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages"
            "(gmail_message_id, thread_id, sender, subject, body, "
            " internal_date_ms, gmail_label_ids, raw_json, first_seen_at) "
            "VALUES (:gmail_message_id, :thread_id, :sender, :subject, "
            " :body, :internal_date_ms, :gmail_label_ids, :raw_json, :first_seen_at)",
            {**row, "first_seen_at": store.now_iso()},
        )
        inserted += cur.rowcount
    conn.commit()  # messages durable BEFORE the checkpoint advances

    # Flow A: record any human relabel of already-triaged mail as a correction,
    # after ingest (a relabel needs its message row) and before the checkpoint —
    # same crash-safety ordering as message ingest.
    correct.detect_relabels(conn, svc, label_events)

    conn.execute(
        "INSERT INTO run_events"
        "(run_id, phase, status, messages_seen, history_id, note, recorded_at) "
        "VALUES (?, 'finished', 'ok', ?, ?, ?, ?)",
        (run_id, len(ids), new_history_id, f"inserted {inserted}", store.now_iso()),
    )
    conn.commit()

    return RunResult(len(ids), inserted, catchup, new_history_id)
