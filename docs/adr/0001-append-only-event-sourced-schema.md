# Append-only, event-sourced SQLite schema

**Status:** accepted (2026-07-13)

Every table in `data/triage.db` is **INSERT-only** — no row is ever UPDATEd or DELETEd. An action's and a run's lifecycle are recorded as immutable **event rows** (`action_events`: intended → confirmed/failed; `run_events`: started → finished), correlated by a `uuid` (`action_id`/`run_id`) with an autoincrement `event_id` as physical log position. "Current state" is *derived* — the latest event per id — exposed through the `current_actions` and `current_checkpoint` views. We chose this for a tamper-evident, replayable audit substrate (the project's cross-cutting auditability requirement) and one uniform mental model, accepting the cost that every "current state" read goes through a latest-event fold rather than a cheap `WHERE status = …`.

## Considered options

- **Mutable status row** (one `actions` row UPDATEd intended→confirmed/failed, with `intended_at`/`confirmed_at` columns). Rejected: mutation-in-place is not tamper-evident, and it left one mutable table in a schema whose other tables (messages, classifications, llm_calls) are already append-only.
- **Spark Structured Streaming** for exactly-once processing. Rejected: exactly-once stops at the sink boundary, so it would not make the external Gmail/Telegram side effects idempotent (the actual concern), and a JVM cluster engine is unjustifiable for ~100 emails/day on a single unattended machine. We kept the append-only *data model* and dropped the *engine*.

## Consequences

- Reads for "what is true now" must use the views / a latest-event fold, not a status column.
- Cost/token records live in a separate `llm_calls` ledger (not columns on `classifications`) so agent digest/chat/draft calls — which have no classification — are still captured.
- `run_id` on `action_events` is a *logical* uuid reference, not a DB-enforced FK (a uuid repeats across an entity's event rows, so it can't be a unique parent column).
