"""Fixed-taxonomy classifier + cost recording (T1.6, issue #12).

The triage path's only LLM call. Given one email, produce exactly one taxonomy
category, one priority, and a one-line reasoning via a single Haiku call, with
tokens + USD cost recorded. Structured output (a JSON-schema `enum`, native on
Haiku 4.5) keeps the model on-taxonomy; the `classifications` CHECK constraint is
the *persistence* guarantee — an off-taxonomy value that slips through raises on
INSERT and is rewritten as UNCLASSIFIED, never silently dropped.

`classify()` is a pure function of an `Email` value so it's testable with a fake
client and reusable by Phase-3 backfill (which sets `batch=True` for the discount).
Re-attempting an UNCLASSIFIED message on a later run is the run loop's job (T1.7).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import anthropic

from assistant import store
from assistant.labels import CATEGORIES, PRIORITIES
from assistant.pricing import PRICES

UNCLASSIFIED = "UNCLASSIFIED"

_RUBRIC_PATH = Path(__file__).with_name("rubric.md")

# Structured-output schema: the model may only return taxonomy-valid values.
_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "reasoning": {"type": "string"},
    },
    "required": ["category", "priority", "reasoning"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Email:
    """The classifier's whole world — decoupled from Gmail and the DB."""

    sender: str
    subject: str
    body: str


@dataclass(frozen=True)
class Verdict:
    category: str
    priority: str | None
    reasoning: str


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int
    output_tokens: int


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, batch: bool = False
) -> float:
    """USD for one call. The API returns token counts but never cost. Batch API
    applies a flat 50% discount."""
    rates = PRICES[model]
    cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
    return cost * 0.5 if batch else cost


def make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def classify(
    client: anthropic.Anthropic, model: str, email: Email
) -> tuple[Verdict, Usage]:
    """One classification attempt. Never raises: any failure — API outage,
    oversized context, malformed response — becomes an UNCLASSIFIED verdict that
    surfaces in `status` for a later retry."""
    prompt = f"From: {email.sender}\nSubject: {email.subject}\n\n{email.body}"
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            system=_RUBRIC_PATH.read_text(),
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except Exception as e:  # noqa: BLE001 — never crash the worker on one email
        return Verdict(UNCLASSIFIED, None, f"api_error: {type(e).__name__}"), Usage(
            model, 0, 0
        )

    usage = Usage(model, resp.usage.input_tokens, resp.usage.output_tokens)
    try:
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
        return Verdict(data["category"], data["priority"], data["reasoning"]), usage
    except (StopIteration, json.JSONDecodeError, KeyError, TypeError) as e:
        return Verdict(
            UNCLASSIFIED, None, f"malformed_response: {type(e).__name__}"
        ), usage


def record(
    conn: sqlite3.Connection,
    gmail_message_id: str,
    verdict: Verdict,
    usage: Usage,
    *,
    actor: str = "worker",
    batch: bool = False,
) -> int:
    """Persist the llm_calls + classifications rows (append-only). On a CHECK
    violation — an off-taxonomy category the schema failed to prevent — rewrite
    the classification as UNCLASSIFIED so the mutation is recorded, not lost."""
    now = store.now_iso()
    cur = conn.execute(
        "INSERT INTO llm_calls"
        "(actor, purpose, model, input_tokens, output_tokens, cost_usd, "
        " gmail_message_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            actor,
            "classify",
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            cost_usd(usage.model, usage.input_tokens, usage.output_tokens, batch),
            gmail_message_id,
            now,
        ),
    )
    llm_call_id = cur.lastrowid
    try:
        _insert_classification(
            conn,
            gmail_message_id,
            verdict.category,
            verdict.priority,
            verdict.reasoning,
            llm_call_id,
            now,
        )
    except sqlite3.IntegrityError:
        _insert_classification(
            conn,
            gmail_message_id,
            UNCLASSIFIED,
            None,
            f"off_taxonomy: {verdict.category!r}",
            llm_call_id,
            now,
        )
    conn.commit()
    return llm_call_id


def _insert_classification(
    conn, gmail_message_id, category, priority, reasoning, llm_call_id, now
):
    conn.execute(
        "INSERT INTO classifications"
        "(gmail_message_id, category, priority, reasoning, llm_call_id, classified_at) "
        "VALUES (?,?,?,?,?,?)",
        (gmail_message_id, category, priority, reasoning, llm_call_id, now),
    )
