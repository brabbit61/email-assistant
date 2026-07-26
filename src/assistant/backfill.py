"""Backfill: cost estimate + the checkpointed Batch API run.

`estimate()` is zero-spend, read-only: it counts what a `--run` would classify
and projects a dollar cost before any spend happens — an up-front gate before
committing to the cost.

`run()` is the actual spend. It's single-step and resumable: each invocation
either polls the one in-flight Batch API request (ingesting its results, labeling
Gmail, then submitting the next page) or, if none is open, submits the next page —
until nothing is left. There is no long-lived process, so a sleeping laptop or a
closed terminal loses nothing; re-running the identical command resumes from the
`classifications` table itself (a message is "done" iff it has a classification
row) and re-polls an in-flight batch by its saved id rather than resubmitting it.

Scope is inbox-only received mail (`in:inbox -in:sent -in:chats`), matching the
worker's own pre-existing backlog rather than all-mail history — shared by both
`estimate()` and `run()` via `_build_query`. Backfilled rows are tagged
`source='backfill'` so they never reach the actionable set (digests/pings/`open`).
The token/cost projection uses the real average from past `classify` calls in
`llm_calls` (self-calibrating to this mailbox and the current rubric) so the
estimate isn't a guess; a built-in constant is the fallback only before any
classification has ever run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from assistant import apply, gmail, store
from assistant.classify import (
    UNCLASSIFIED,
    Email,
    Usage,
    Verdict,
    cost_usd,
    record,
    request_params,
    verdict_from_message,
)

# ponytail: a rough per-email average, used only until llm_calls has real
# classify rows to average — bump if the rubric/typical email size shifts.
DEFAULT_AVG_INPUT_TOKENS = 1500
DEFAULT_AVG_OUTPUT_TOKENS = 50


@dataclass(frozen=True)
class EstimateResult:
    message_count: int
    already_classified: int
    avg_input_tokens: int
    avg_output_tokens: int
    basis_count: int
    est_cost_usd: float
    model: str
    scope_label: str


def _build_query(after: str | None, before: str | None) -> str:
    q = "in:inbox -in:sent -in:chats"
    if after:
        q += f" after:{after.replace('-', '/')}"
    if before:
        q += f" before:{before.replace('-', '/')}"
    return q


def _scope_label(after: str | None, before: str | None) -> str:
    if after or before:
        return f"{after or '…'} → {before or '…'}"
    return "full history (no date bound)"


_QUERY_CHUNK = 500  # stays under SQLite's variable limit even on old builds (999)


def _already_classified(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), _QUERY_CHUNK):
        chunk = ids[i : i + _QUERY_CHUNK]
        rows = conn.execute(
            "SELECT DISTINCT gmail_message_id FROM classifications "
            "WHERE gmail_message_id IN ({})".format(",".join("?" * len(chunk))),
            chunk,
        )
        found.update(r[0] for r in rows)
    return found


def _token_average(conn: sqlite3.Connection) -> tuple[int, int, int]:
    row = conn.execute(
        "SELECT COUNT(*), AVG(input_tokens), AVG(output_tokens) FROM llm_calls "
        "WHERE purpose = 'classify'"
    ).fetchone()
    count = row[0]
    if not count:
        return DEFAULT_AVG_INPUT_TOKENS, DEFAULT_AVG_OUTPUT_TOKENS, 0
    return round(row[1]), round(row[2]), count


def estimate(
    conn: sqlite3.Connection,
    svc,
    model: str,
    after: str | None,
    before: str | None,
) -> EstimateResult:
    ids = gmail.list_message_ids(svc, _build_query(after, before))
    already = _already_classified(conn, ids)

    avg_in, avg_out, basis_count = _token_average(conn)
    to_classify = len(ids) - len(already)
    cost = cost_usd(model, avg_in * to_classify, avg_out * to_classify, batch=True)

    return EstimateResult(
        message_count=to_classify,
        already_classified=len(already),
        avg_input_tokens=avg_in,
        avg_output_tokens=avg_out,
        basis_count=basis_count,
        est_cost_usd=cost,
        model=model,
        scope_label=_scope_label(after, before),
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_estimate(r: EstimateResult) -> str:
    total_input = r.avg_input_tokens * r.message_count
    total_output = r.avg_output_tokens * r.message_count
    basis = (
        f"basis: {r.basis_count:,} prior classifications"
        if r.basis_count
        else "basis: PLAN default, no prior classifications yet"
    )
    lines = [
        "Backfill estimate — inbox, received mail only",
        f"{'Scope:':<14}{r.scope_label}",
        f"{'Messages:':<14}{r.message_count:,} to classify  "
        f"({r.already_classified:,} already classified, skipped)",
        f"{'Est. tokens:':<14}~{_fmt_tokens(total_input)} input / "
        f"~{_fmt_tokens(total_output)} output  "
        f"(avg {r.avg_input_tokens:,} in / {r.avg_output_tokens:,} out per msg,",
        f"{'':<14}{basis})",
        f"{'Est. cost:':<14}~${r.est_cost_usd:.2f}   "
        f"({r.model}, Batch API 50% discount)",
        "",
        "Zero spent — estimate only. Run `assistant backfill --run` to execute.",
    ]
    return "\n".join(lines)


# --- run: checkpointed Batch API backfill -------------------
#
# One invocation makes exactly one state transition, so it always terminates
# quickly (no in-process blocking across the Batch API's minutes-to-hours
# turnaround): poll the single in-flight batch if there is one — and if it has
# ended, ingest + label + submit the next page — otherwise submit the next page,
# else report completion. Resume is re-derived from `classifications` each run.

# messages per Batch API request. ponytail: a page's bodies stay far under the
# API's 256 MB / 100k-request caps (1000 x ~10 KB ~= 10 MB); a module constant,
# not config — promote only if a mailbox ever needs per-run tuning.
_PAGE_SIZE = 1000


@dataclass
class RunResult:
    scope_label: str
    polled_batch: str | None = None
    processing: bool = False  # polled a batch that hasn't ended yet
    progress: tuple[int, int] = (0, 0)  # (done, total) for a still-processing batch
    submitted_age: str | None = None  # "Nm ago" since this batch was submitted
    # ingest tally for a batch that ended this run: succeeded, errored, skipped, labeled
    counts: tuple[int, int, int, int] = (0, 0, 0, 0)
    submitted_batch: str | None = None
    submitted_count: int = 0
    complete: bool = False  # nothing left to classify in scope


def _open_batch(conn: sqlite3.Connection) -> tuple[str, str, str] | None:
    """The one submitted-but-not-yet-ingested backfill batch: (run_id, batch_id,
    submitted_at), or None. A batch is open until a `page_done` event names the
    same batch_id. submitted_at feeds the "still processing" elapsed-time display
    — the one thing guaranteed to change between polls even when the Batch API's
    request_counts doesn't move until the whole batch ends (follow-up)."""
    row = conn.execute(
        "SELECT run_id, batch_id, recorded_at FROM run_events "
        "WHERE phase = 'backfill' AND status = 'submitted' AND batch_id NOT IN ("
        "  SELECT batch_id FROM run_events "
        "  WHERE phase = 'backfill' AND status = 'page_done' AND batch_id IS NOT NULL) "
        "ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def _next_page(
    conn: sqlite3.Connection, svc, after: str | None, before: str | None
) -> list[str]:
    """The next page of in-scope message ids with no classification row yet."""
    ids = gmail.list_message_ids(svc, _build_query(after, before))
    already = _already_classified(conn, ids)
    return [i for i in ids if i not in already][:_PAGE_SIZE]


def _is_classified(conn: sqlite3.Connection, mid: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM classifications WHERE gmail_message_id = ? LIMIT 1", (mid,)
        ).fetchone()
        is not None
    )


def _submit_page(
    conn: sqlite3.Connection, svc, client, model: str, ids: list[str]
) -> str:
    """Fetch each message (persist to `messages` for the classifications FK), submit
    them as one Batch API request, and record a `submitted` checkpoint carrying the
    batch id. custom_id = gmail_message_id maps results back on ingest.

    origin='backfill' keeps the row out of the live `assistant run` loop even
    while a result is still pending (e.g. expired/canceled, which deliberately
    gets no classification row so a later page can retry it) — the live loop
    excludes non-'poll' origin unconditionally, not just unclassified rows."""
    run_id = store.new_id()
    now = store.now_iso()
    requests = []
    for mid in ids:
        msg = gmail.get_message(svc, mid)
        conn.execute(
            "INSERT OR IGNORE INTO messages"
            "(gmail_message_id, thread_id, sender, subject, body, internal_date_ms, "
            " gmail_label_ids, raw_json, first_seen_at, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,'backfill')",
            (
                msg["gmail_message_id"],
                msg["thread_id"],
                msg["sender"],
                msg["subject"],
                msg["body"],
                msg["internal_date_ms"],
                msg["gmail_label_ids"],
                msg["raw_json"],
                now,
            ),
        )
        requests.append(
            {
                "custom_id": mid,
                "params": request_params(
                    model,
                    Email(msg["sender"] or "", msg["subject"] or "", msg["body"] or ""),
                ),
            }
        )
    conn.commit()

    batch = client.messages.batches.create(requests=requests)
    conn.execute(
        "INSERT INTO run_events"
        "(run_id, phase, status, batch_id, messages_seen, recorded_at) "
        "VALUES (?, 'backfill', 'submitted', ?, ?, ?)",
        (run_id, batch.id, len(ids), store.now_iso()),
    )
    conn.commit()
    return batch.id


def _batch_error(item) -> str:
    """Short error type for an `errored` result, best-effort (informational)."""
    err = getattr(item.result, "error", None)
    return getattr(getattr(err, "error", None), "type", "unknown")


def _ingest_batch(
    conn: sqlite3.Connection, svc, client, run_id: str, batch_id: str, model: str
) -> tuple[int, int, int, int]:
    """Stream an ended batch's results into `classifications` (+ Gmail category
    labels), committing per message so an interrupted ingest resumes on the
    remainder. Idempotent: a message already classified (a prior crashed ingest)
    is skipped. Returns (succeeded, errored, skipped, labeled).

    Failure policy (locked): succeeded -> record + label; errored -> record
    UNCLASSIFIED (terminal, so a poisoned message can't loop the backfill forever);
    expired/canceled -> skip, leaving it for a later page to retry.

    Priority is stripped before it's ever stored, not just before the Gmail
    label — backfill is category-only end to end, so a P1/P2 verdict on
    old mail never lands in `classifications.priority` for anything downstream
    to notice, even a future query that (unlike today's) forgets to filter
    source='backfill'."""
    succeeded = errored = skipped = labeled = 0
    for item in client.messages.batches.results(batch_id):
        mid = item.custom_id
        if _is_classified(conn, mid):
            continue
        rtype = item.result.type
        if rtype == "succeeded":
            message = item.result.message
            raw = verdict_from_message(message)
            verdict = Verdict(raw.category, None, raw.reasoning)
            usage = Usage(
                model, message.usage.input_tokens, message.usage.output_tokens
            )
            record(
                conn,
                mid,
                verdict,
                usage,
                actor="backfill",
                source="backfill",
                batch=True,
            )
            succeeded += 1
            if verdict.category != UNCLASSIFIED:
                try:
                    apply.apply_verdict(
                        conn,
                        svc,
                        run_id,
                        mid,
                        verdict,
                        actor="backfill",
                        auto_archive=False,
                    )
                    labeled += 1
                except Exception:  # noqa: BLE001 — one label failure can't abort the page
                    pass
        elif rtype == "errored":
            record(
                conn,
                mid,
                Verdict(UNCLASSIFIED, None, f"batch_error: {_batch_error(item)}"),
                Usage(model, 0, 0),
                actor="backfill",
                source="backfill",
                batch=True,
            )
            errored += 1
        else:  # expired / canceled — not the message's fault; retry on a later page
            skipped += 1

    conn.execute(
        "INSERT INTO run_events"
        "(run_id, phase, status, batch_id, messages_seen, actions_taken, error_count, "
        " recorded_at) VALUES (?, 'backfill', 'page_done', ?, ?, ?, ?, ?)",
        (run_id, batch_id, succeeded, labeled, errored, store.now_iso()),
    )
    conn.commit()
    return succeeded, errored, skipped, labeled


def run(
    conn: sqlite3.Connection,
    svc,
    client,
    model: str,
    after: str | None,
    before: str | None,
) -> RunResult:
    """One resumable backfill step. See module docstring for the state machine."""
    result = RunResult(scope_label=_scope_label(after, before))

    open_batch = _open_batch(conn)
    if open_batch:
        run_id, batch_id, submitted_at = open_batch
        result.polled_batch = batch_id
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            rc = batch.request_counts
            done = rc.succeeded + rc.errored + rc.expired + rc.canceled
            result.processing = True
            result.progress = (done, done + rc.processing)
            result.submitted_age = store.age(submitted_at)
            return result  # single batch in flight — don't submit a second
        result.counts = _ingest_batch(conn, svc, client, run_id, batch_id, model)

    # No open batch (or we just ingested one) — submit the next page.
    ids = _next_page(conn, svc, after, before)
    if not ids:
        result.complete = True
        return result
    result.submitted_batch = _submit_page(conn, svc, client, model, ids)
    result.submitted_count = len(ids)
    return result


def format_run(r: RunResult) -> str:
    # Elapsed time leads, not the done/total fraction: the Batch API's
    # request_counts often doesn't move at all until the whole batch ends, so a
    # bare "0/1,000" on every poll is noise (the same line, no new information).
    # "Nm ago" always changes — it's the one signal a repeated poll can trust.
    lines = [f"Backfill run — {r.scope_label}"]
    if r.processing:
        done, total = r.progress
        progress = f"{done:,}/{total:,} done, " if done else ""
        lines.append(
            f"  Batch {r.polled_batch}: {progress}still processing "
            f"(submitted {r.submitted_age}) — re-run to poll."
        )
        return "\n".join(lines)
    if r.polled_batch:  # an ended batch we ingested this run
        s, e, sk, lab = r.counts
        lines.append(
            f"  Ingested batch {r.polled_batch}: {s:,} classified ({lab:,} labeled), "
            f"{e:,} errored, {sk:,} skipped for retry."
        )
    if r.submitted_batch:
        lines.append(
            f"  Submitted {r.submitted_count:,} message(s) as batch "
            f"{r.submitted_batch} — re-run to poll."
        )
    if r.complete:
        lines.append("  Nothing left to classify — backfill complete for this scope.")
    return "\n".join(lines)
