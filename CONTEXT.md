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
