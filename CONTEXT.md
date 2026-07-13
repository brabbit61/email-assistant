# Email Assistant

A personal Gmail triage worker plus a hermes-agent conversational layer, run by one user (Jenit) on his own machines. See PLAN.md for the design; this file is the project's vocabulary.

## Language

### Configuration

**Secret**:
A credential that grants access to an external service (API key, OAuth client, OAuth token, bot token). Lives only in `secrets/`, never committed.
_Avoid_: config, env var, key (alone)

**Tunable**:
A non-secret setting a human may adjust (model ids, budget caps, digest times, poll interval). Lives in the committed `config.toml`.
_Avoid_: setting, option, parameter

**State**:
Data the system produces and must not lose — above all `triage.db`. Lives in `data/`, gitignored, created at runtime.
_Avoid_: cache, artifacts

### Triage

**Triage worker**:
The deterministic Python pipeline that polls Gmail, classifies each new message, and applies labels. No agent loop.
_Avoid_: bot, agent (that's the hermes layer)

**Taxonomy**:
The fixed set of 8 category labels + 3 priority labels the classifier must choose from. The system may never invent labels.
_Avoid_: categories, tags

**Digest**:
The thrice-daily Telegram summary composed by the hermes agent from triage data.
_Avoid_: report, notification (that's an urgent ping)

**Urgent ping**:
An immediate Telegram message for P1 mail, sent directly by the worker — never dependent on the agent being healthy.
_Avoid_: alert, digest

### Data model (triage.db)

**Message**:
A Gmail message the worker has seen, identified by its Gmail message id. We store metadata only — sender, subject, snippet, thread, timestamps — never the full body.
_Avoid_: email, mail (informal only), record

**Classification**:
One verdict about a Message — its category, optional priority, and the model's one-line reasoning. Append-only: a Message may be re-classified, and the latest Classification is the current verdict. Older ones are kept.
_Avoid_: label (that's the Gmail-side artifact), tag, verdict

**Action**:
One logical mutation the system makes to the outside world — a single label add/remove, archive, draft-create, or Telegram send — identified by an `action_id`. Its lifecycle is recorded as immutable **action events** (`intended`, then `confirmed` or `failed`), appended to `action_events` and never updated in place. The `intended` event is written before the external call; nothing mutates Gmail or Telegram without it.
_Avoid_: mutation, operation

**Actor**:
Who initiated an Action or LLM Call: `worker`, `agent`, or `backfill`.
_Avoid_: source, origin, user

**LLM Call**:
One request to an Anthropic model by any component (classify, digest, chat, draft, backfill), recorded with model, token counts, and the USD cost computed at call time. The `llm_calls` table is the **cost ledger** — the single source for all spend.
_Avoid_: cost row, api call, usage

**Run**:
One poll-and-triage cycle of the worker, identified by a `run_id`. Recorded as immutable **run events** (`started`, then `finished` with totals), appended to `run_events` and never updated in place. The **checkpoint** is the Gmail `historyId` of the latest `finished` run event with status `ok` — where processing resumes.
_Avoid_: cycle, job, checkpoint (checkpoint = the historyId value, not the Run)
