"""Backfill cost estimate (T3.4, issue #68). Zero-spend, read-only: counts what
a `--run` would classify and projects a dollar cost before any spend happens —
the up-front gate PLAN.md's Costs section requires.

Scope is inbox-only received mail (`in:inbox -in:sent -in:chats`), matching the
worker's own pre-existing backlog rather than all-mail history. The token/cost
projection uses the real average from past `classify` calls in `llm_calls`
(self-calibrating to this mailbox and the current rubric) so the estimate isn't
a guess; a PLAN.md constant is the fallback only before any classification has
ever run. Output format locked in S3.1 (#64).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from assistant import gmail
from assistant.classify import cost_usd

# ponytail: PLAN.md's rough per-email average, used only until llm_calls has
# real classify rows to average — bump if the rubric/typical email size shifts.
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
        "Zero spent — estimate only. "
        "Run `assistant backfill --run --confirm` to execute.",
    ]
    return "\n".join(lines)
