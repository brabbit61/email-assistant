# Progress Log

Running record of completed tickets: what was done, how, and what was decided. Kept up to date as tickets close; feeds the README rewrite at project end. Full ticket conventions and rationale live in [PLAN.md](PLAN.md) — this file is the "what actually happened" complement to that plan.

---

## Phase 0: Prerequisites

### #1 — T0.1 Tracking setup: labels, milestones, ticket conventions
**Status:** Closed

Set up the GitHub tracking system the project runs on: six milestones (Phase 0–5), the label vocabulary (`type:setup|feature|infra|spec|docs`, `needs-signoff`, `user-task`, `blocked`, `epic`), and the ticket template (Goal / Context / Scope / Acceptance criteria / Decisions requiring sign-off / Blocked by), documented in [PLAN.md](PLAN.md).

**Decided:** Ticket conventions and label vocabulary approved as-is. No GitHub Projects board (token lacks `project` scope; not needed for a solo project).

### #2 — T0.2 Google Cloud project + Gmail API OAuth desktop credentials
**Status:** Closed

Jenit created a Google Cloud project, enabled the Gmail API, and generated OAuth desktop-app client credentials for `jenitjain10@gmail.com`. Credential JSON stored outside the repo (final path to be fixed by the T0.6 config convention).

**Decided:**
- OAuth consent screen: **External**, Testing mode, Jenit added as test user — app never goes through Google verification since it's personal-use only.
- Scope limited to `gmail.modify` — permits read/label/archive/draft but **not** permanent deletion. This is the technical enforcement of the project's "never delete" guardrail.
- Only the Gmail API is enabled for now; Calendar scope deferred to Phase 4.

### #3 — T0.3 Anthropic API key + monthly budget cap
**Status:** Closed

Jenit created an Anthropic API key (`email-assistant`) and set a console-level hard spend limit. Key stored outside the repo (final path to be fixed by the T0.6 config convention).

**Decided:**
- Monthly budget cap: **$15/month** console hard limit, with a **$0.75/day** soft cap to be enforced by the worker itself (implemented later, Phase 2).
- Model split confirmed: **Haiku** for per-email classification (cheap, high volume), **Sonnet** for the hermes agent (digests, chat, drafting).

### #4 — T0.4 Create Telegram bot + capture chat id
**Status:** Closed

Created the Telegram bot via @BotFather, retrieved Jenit's numeric chat id via @userinfobot (faster than the `getUpdates` API route, which requires a message to already be queued), and confirmed delivery with a manual `sendMessage` test.

**Decided:**
- Bot display name: **Brabbit**, handle: **@BrabbitEmailBot**.
- Single-user lockdown approved: the bot will only ever respond to Jenit's chat id. Enforcement lands as a config value once T0.6 (repo scaffolding) defines where config lives.

**How it was verified:** manual `curl -X POST https://api.telegram.org/bot<token>/sendMessage` reached Jenit's phone.

**Note:** token + chat id are currently held by Jenit directly (not yet in a config file) — they move into the repo's secrets convention once #6 lands.

---

### #5 — T0.5 Install hermes-agent on Linux desktop, pin version
**Status:** Awaiting sign-off (installed, unverified decisions posted to issue)

Ran the official installer (`install.sh --branch v2026.7.7.2 --skip-browser --non-interactive`), pinned to tag **v2026.7.7.2** (commit `9de9c25`). Prerequisites: Python 3.12.3, Node v18.19.1, ripgrep 14.1.1 already present; ffmpeg and the Python venv were installed by the script. Browser/Playwright install was skipped (not needed for triage; Phase 2 gateway/skills work can add it later).

Configured `~/.hermes/config.yaml` for the direct Anthropic provider (`model.provider: anthropic`, `model.default: claude-sonnet-5`) and copied the Anthropic key from the repo's T0.3 key into `~/.hermes/.env`. Verified with `hermes chat -q "..."` — got a correct reply.

**Decided (pending Jenit's sign-off, posted as issue comment):**
- Pinned version: v2026.7.7.2 (latest stable at execution time).
- Install layout: default (`~/.hermes` for config/data/code, `~/.local/bin/hermes` launcher) — no reason to customize for a single-machine dev setup.
- Model config: Sonnet 5 via direct Anthropic API, confirming T0.3's model split.

**Note:** the repo's `.env` holds the Anthropic key under `MEMORYLANE_ANTHROPIC_API_KEY` — a leftover name from another project, not `email-assistant`. Worth renaming once T0.6 lands the real secrets convention.

### #6 — T0.6 Repo scaffolding + tech-stack sign-off
**Status:** Closed

Grilled every tech-stack decision one-by-one, then scaffolded the repo: `pyproject.toml` (src layout, `assistant` entry-point stub, ruff config), `.python-version`, committed `config.toml` with the signed-off tunables, `hermes/` + `deploy/` skeleton dirs, rewritten README (bootstrap + secrets convention), and `CONTEXT.md` (project glossary: secret / tunable / state, triage terms). Existing `.env` and Google client JSON moved into `secrets/` (renamed to stable `client_secret.json`, dir chmod 700).

**Decided:**
- **uv** for venv + deps + Python installs; `uv.lock` committed. Bootstrap = `uv sync`.
- **Python 3.13** (upgraded from the ticket's 3.12 rec — one extra year of support runway, zero extra cost since uv installs it anywhere).
- **Secrets stay in the repo folder** (Jenit's call, against the ~/.config recommendation), hardened by consolidating everything into `secrets/`, ignored as a whole directory with a single `/secrets/` line so new credential files are auto-covered. Runtime state (`triage.db`) gets the same treatment in `data/`.
- **config.toml (committed) + secrets/.env** split: tunables vs credentials.
- **ruff** for lint + format (`extend-select = ["I"]`), dev dep alongside pytest.
- **Runtime deps minimal:** google-api-python-client, google-auth-oauthlib, anthropic. `.env` parsing and Telegram sendMessage use stdlib — no python-dotenv, no requests.

**How it was verified:** fresh `uv sync` → `uv run assistant` resolves the entry point; `ruff check` + `ruff format --check` clean; `config.toml` parses with stdlib `tomllib`; `git check-ignore` confirms both secret files are covered by the `/secrets/` rule.

**Note:** `secrets/.env` still contains stale sections copied from another project's template (Google Photos / MinIO / S3 / LangSmith) — the live keys are `EMAIL_GOOGLE_CLIENT_ID/SECRET`, `EMAIL_ANTHROPIC_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. Worth pruning when T1.1 (config loading, #7) fixes the canonical key names.

**Scope change (2026-07-11, post-close):** Jenit reversed the "no CI" decision. Requirements + use cases posted as a comment on #6; `.github/workflows/ci.yml` added with two jobs: `checks` (uv sync --locked → ruff check → ruff format --check → entry-point smoke run → pytest once tests exist) and `secret-scan` (gitleaks over full history — insurance on the secrets-in-repo decision). CD stays pull-based and lands with Phase 5 (#21): the box pulls `main` on a timer; no self-hosted runners.

---

## Phase 1: Triage worker MVP

### #7 — T1.1 Config & secrets loading
**Status:** Closed

Built `src/assistant/config.py` — the single place every later component reads config + secrets. Loads `config.toml` via stdlib `tomllib` and `secrets/.env` via a ~8-line stdlib KEY=VALUE parser (no python-dotenv, per #6). Returns a frozen `Config` (resolved paths for client_secret.json / token.json / data/triage.db, model ids, budget caps, poll interval, digest times) holding a frozen `Secrets`. Added committable `.env.example` at repo root and `tests/test_config.py` (4 cases).

**Decided (no sign-off required — implements T0.6):**
- **Canonical secrets = 3 keys:** `EMAIL_ANTHROPIC_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. Google OAuth is path-based (`secrets/client_secret.json`), not env — so the stale `EMAIL_GOOGLE_*` / `EMAIL_LANGSMITH_*` keys in the live `.env` are now **inert** (loader ignores unknown keys); can be hand-pruned anytime.
- **Fail-loudly:** missing/empty required secret raises `ConfigError` naming the key; `config.toml` missing/malformed raises a clear `ConfigError` too.
- **No value leaks structurally:** `Secrets.__repr__` masks all three values, so a secret can't reach a log/traceback even if the config is printed.
- **Root resolution:** explicit `home=` arg (test seam) → `EMAIL_ASSISTANT_HOME` env (systemd/WSL2) → search upward from CWD for `config.toml`. Works from any subdirectory.

**How it was verified:** `uv run pytest` → 4/4 green (valid load w/ quote+comment parsing and unknown-key tolerance, missing-secret names key without leaking a present value, masked repr, clear error on absent config.toml); ruff clean; live `load()` against the real repo printed the actual tunables, resolved db path, confirmed `client_secret.json` exists, loaded the real 108-char Anthropic key, and rendered `Secrets(...)` masked.

### #8 — T1.2 SQLite schema
**Status:** Closed

Grilled the schema design one decision at a time (schema doc posted + approved on the issue before implementation, per the ticket). Built `src/assistant/store.py`: five tables + two views in `data/triage.db`, `PRAGMA user_version` migrations (no ORM), WAL + busy_timeout + foreign_keys pragmas, `now_iso()`/`new_id()` helpers, `open_db()`. Added `tests/test_store.py` (6 cases) and ADR 0001.

**Decided (grilled):**
- **Fully append-only / event-sourced.** Every table is INSERT-only; nothing is ever UPDATEd. Started from a mutable-status `actions` table and, after grilling, converted the whole schema to immutable event rows: `action_events` (intended→confirmed/failed) and `run_events` (started→finished), each correlated by a `uuid` (`action_id`/`run_id`) with autoincrement `event_id` as log position. Current state derived via `current_actions` / `current_checkpoint` views.
- **Notable detour:** Jenit initially wanted Spark Structured Streaming for exactly-once idempotency. Grilled it down — exactly-once stops at the sink boundary (doesn't make Gmail/Telegram side effects once-only) and a JVM engine is unjustified at ~100 emails/day. Outcome: "take the model, drop the engine" — append-only in plain SQLite. (Saved as a durable memory.)
- **Separate `llm_calls` cost ledger** (not cost columns on classifications) so agent digest/chat/draft calls are captured — deliberate divergence from the ticket wording, recorded in ADR 0001.
- Self-describing event rows; `run_id` a logical uuid ref (not a hard FK); category/priority free-text validated in Python; timestamps UTC ISO-8601; `cost_usd` stored at call time.
- Also bounded `uv_build` in `pyproject.toml` (`>=0.11,<0.12`) to silence the build warning.

**How it was verified:** `uv run pytest` → 10/10 green (schema+views created, user_version==1, WAL + foreign_keys on, migration idempotent, action intended→confirmed derives one `current_actions` row = confirmed, `current_checkpoint` picks latest ok finish, FK violation raises). ruff clean. Live `open_db(config.db_path)` created `data/triage.db` at version 1 with all 5 tables + 2 views (stray runtime db then removed; it's gitignored).

### #9 — T1.3 Gmail OAuth flow + token persistence/refresh
**Status:** Closed

Built `src/assistant/gmail.py`: one entry point `get_credentials(config, *, interactive=False)` over the desktop-app OAuth client (`gmail.modify` scope only). Loads `secrets/token.json` if present; returns it valid, silently refreshes it if expired (persisting the rotated token), runs the browser consent flow only when `interactive=True`. The documented auth command is `uv run python -m assistant.gmail`; re-auth = delete `token.json` and re-run. Added `tests/test_gmail.py` (6 cases, network/browser fully stubbed) and README "Gmail authorization" section. No sign-off decisions (implements #2).

**Decided (no sign-off — implements #2/#7):**
- **Non-interactive by default.** The unattended worker calls `get_credentials(config)` and never blocks on a browser: a valid/refreshable token returns silently, anything needing a human raises `AuthError`. Only the manual auth command passes `interactive=True`.
- **Loud, not silent, on permanent failure.** A `RefreshError` (revoked / password change / dead refresh token) becomes `AuthError`; the auth command records an append-only `run_events` row (`phase='auth'`, `status='error'`, note) via the T1.2 store and exits nonzero — no new table, and it surfaces in `assistant status` (#14). Telegram alerting stays Phase 2.
- **Token is a durable secret.** `token.json` written 0600 inside the already-0700 `secrets/` dir. Refreshed tokens are persisted back so rotation survives restarts.
- **Store stays decoupled on the happy path.** `gmail.py` imports the DB lazily, only inside the failure-recording helper — auth doesn't touch SQLite when it succeeds.

**How it was verified:** `uv run pytest` → 16/16 green (no-token non-interactive raises; valid token returned without refresh/flow; expired token refreshes + persists; permanent refresh failure raises `AuthError`; interactive first run persists 0600; failure recorded in `run_events`). ruff clean. The live browser flow itself is inherently interactive (Jenit runs it once) — not exercised in CI.

### #10 — T1.4 Create taxonomy labels in Gmail (idempotent)
**Status:** Implemented, pending Jenit's visual sign-off (open)

Grilled all three sign-off decisions. Built `src/assistant/labels.py`: one taxonomy definition (`CATEGORIES`/`PRIORITIES`, importable by the classifier #12), a `LabelSpec` per label (name, color, list visibility), and `reconcile(svc) -> ReconcileResult` — an idempotent upsert (`labels.list` once, then create-if-missing / patch-if-drifted / skip-if-matching). `main()` is `uv run python -m assistant.labels`, reusing the #9 auth path (non-interactive). Added a small `service(creds)` builder to `gmail.py` for reuse by later Gmail tickets. Added `tests/test_labels.py` (fake in-memory Gmail service, no network).

**Decided (grilled):**
- **Taxonomy widened 8 → 11 categories.** Added Work, Bills, Dev to fit Jenit's profile (data/ML engineer); rejected Receipts (folds into Orders/Bills), Health, Learning, Social, standalone Security as too thin to wall off. Updated PLAN.md's locked taxonomy row and issue #12 (classifier) to match.
- **One collapsible parent (`Assistant`)**, not flat or two-parent — full names are `Assistant/<emoji> <name>`.
- **Emoji on every label** (all 14), for mobile scannability where the color chip is small.
- **Color grouped by family**, not full rainbow: priorities are traffic-light (red/amber/blue), Action-Needed is hot orange, the 11 categories are muted tones grouped by domain (money/logistics/people/machine) — so urgency pops and the sidebar doesn't shout.
- **Priorities `labelShowIfUnread`**, categories always `labelShow` — priorities surface only when something in that bucket is unread; categories are a permanent nested list (already contained by the collapsible parent).
- **No label-id table in the DB** (dropped from ticket scope) — the applier (#13/T1.7) resolves `name → id` live from `labels.list` at startup; Gmail stays the single source of truth, no staleness risk if a label is ever deleted/recreated.

**How it was verified:** `uv run pytest` → 19/19 green (first reconcile creates all 15; second reconcile is fully idempotent — 0 created/updated; a drifted color patches only that one label). ruff clean. **Live:** ran `uv run python -m assistant.labels` against Jenit's real Gmail — first run created 14 labels (+ parent), second run reported 0 created / 0 updated / 14 unchanged; a follow-up `labels.list` readback confirmed all 15 names/colors/visibility match spec exactly. **Outstanding:** Jenit still needs to look at the sidebar (web + mobile) and confirm the labels are visually distinguishable before this closes — the one acceptance criterion that isn't a repo-side check.

### #13 — T1.7 Label applier with audit-before-write guarantee
**Status:** Closed

Closed on GitHub back on 2026-07-13 with nothing actually built — flagged during #14 planning (no `apply`/`archive` function anywhere in the repo, no commit, no branch) and reopened in substance to build it for real. Built `src/assistant/apply.py`: `apply_verdict(conn, svc, run_id, gmail_message_id, verdict, ...) -> ApplyResult`. One `action_id` per label (category, priority when present, archive when enabled), but exactly one combined `messages.modify` Gmail call per message — every `intended` `action_events` row precedes it, every `confirmed`/`failed` row follows it. Added `labels.label_ids(svc)` (live name→id lookup, no create/patch, per #10's decision) and a small `labels.FULL_NAME` key→full-name map.

**Decided:**
- **`dry_run=True` skips the Gmail call *and* every `action_events` write**, not just the Gmail call — an `intended`-only row with no eventual terminal event would be indistinguishable from a genuine mid-crash, and a dry run isn't one.
- **Archive gated by a new `auto_archive_low_value` config tunable** (`config.toml [triage]`, default `false`) — Phase 3 flips it on, as originally scoped.
- **Label ids resolved before any audit write**, not after — a missing/unreconciled label is a setup problem (taxonomy not reconciled), not a per-message crash, so it raises `KeyError` with zero dangling `action_events` rows rather than leaving a permanently-unconfirmed intent behind.
- Idempotent re-application (crash-reprocess) gets a **fresh `action_id`** each time, not the same one — an extra log row, not a bug, consistent with ADR 0001's "retry = new intent" convention.

**How it was verified:** `uv run pytest tests/test_apply.py` → 7/7 green (category+priority in one combined call; dry-run touches neither Gmail nor the audit log; archive on/off gating; a Gmail failure leaves matching `failed` terminal events and re-raises for the caller to isolate; a missing label raises before any audit write). ruff clean.

### #14 — T1.8 `assistant` CLI: `run --dry-run`, `status`, `audit`, `costs`
**Status:** Closed

Rewrote `src/assistant/cli.py` (previously a scaffolding stub) with stdlib `argparse` — no click/typer, consistent with the repo's repeated minimal-deps decisions (#6/#7/#9). `run` composes `poll.poll_once` → `classify.classify`/`record` → `apply.apply_verdict` into one pass, retrying any message whose latest classification is still `UNCLASSIFIED` (via a new `current_classifications` view, schema v2, mirroring `current_actions`/`current_checkpoint`) and isolating per-message failures so one poisoned message doesn't abort the batch. `status`/`audit`/`costs` are read-only reports over the same tables.

**Decided (output formats signed off on issue #14, samples posted as a comment):**
- **`run`'s own run-summary row uses `run_events(phase='triage', ...)`, not `phase='finished'`.** `current_checkpoint` matches on `phase='finished' AND status='ok'` without checking `history_id IS NOT NULL` — a same-named summary row from the CLI would race `poll.py`'s own checkpoint row for "latest event_id" and could null out the poller's resume point. `phase='triage'` is a value only the CLI writes.
- **Cost figures show 4 decimal places throughout** (total, cap, per-line, daily average/cap, daily breakdown) — 2dp rounded a single Haiku classify call (~$0.001) to $0.00, hiding real spend. Changed after Jenit's review of the sample output.
- **`audit` prints one line per action's *current* state** (via `current_actions`), not a doubled `intended`+`confirmed` row per action — caught during live testing, where the trailing summary line was misreporting confirmed actions as "pending" because it tallied raw event rows instead of derived state.
- Exit codes: `run` and `status` are nonzero on any unhealthy condition (per-message errors; no checkpoint; last run errored; any action stuck `intended`); `audit`/`costs` are informational (0 unless a hard DB/config error). Bare `assistant` (no subcommand) still prints usage and exits 0, preserving CI's smoke-test step.
- `SCHEMA_VERSION` (`store.py`) changed from a hand-maintained literal to `len(_MIGRATIONS)` — the literal drifted out of sync twice while the `current_classifications` migration was being added.

**How it was verified:** `uv run pytest` → 47/47 green (includes 9 new `tests/test_cli.py` cases: happy-path run, dry-run writes zero Gmail mutations but still records cost, UNCLASSIFIED retry, per-message failure isolation, status health/unhealth, audit reasoning + `--since` filter, costs aggregation, bare-command exit). ruff clean. All four commands' sample output in the #14 sign-off comment is genuine executed output (seeded demo DB + faked Gmail/Anthropic edges), not a hand-typed mockup.

## Phase 2: Telegram + hermes

### #41 — T2.1 Hermes Telegram gateway: bind bot, lock to chat id, verify two-way chat
**Status:** Closed

Bound hermes-agent's messaging gateway to the Phase 0 Telegram bot (#4) and locked it to Jenit's chat id. `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS=<Jenit's chat id>` added to hermes's own `~/.hermes/.env`; `unauthorized_dm_behavior: ignore` added to `~/.hermes/config.yaml`. Installed and started as a systemd **user** service via `hermes gateway install` (mirrors the worker's existing timer pattern; linger already enabled). Runbook at `deploy/hermes-gateway.md`.

**Decided:**
- **Lockdown = the `TELEGRAM_ALLOWED_USERS` allowlist env var, not `dm_policy`.** Telegram has no `dm_policy` setting in hermes (that's a WhatsApp/WeCom/Weixin construct) — the allowlist is the entire access-control mechanism for Telegram, and it's fail-closed, enforced at message intake before the agent ever sees the message.
- **Config lives in hermes's own files, not this repo's `secrets/.env`.** The bot token is duplicated under two different env var names in two separate `.env` files (worker's `TELEGRAM_TOKEN`, hermes's `TELEGRAM_BOT_TOKEN`) — accepted as the cost of two independent processes; not plumbed together.
- **`unauthorized_dm_behavior: ignore`** (not the default `pair`) — a non-allowlisted sender gets total silence, matching the ticket's "ignored" acceptance criterion exactly, rather than a pairing-request prompt.
- **One shared bot token confirmed safe with the worker's future send-only path (#42).** Telegram caps `getUpdates` long-polling at one consumer per token — the gateway is that consumer; `sendMessage` (the worker's whole job in #42) has no such cap. Constraint written into #42: the worker must never call `getUpdates` or register a webhook.
- **Round-trip test scoped to transport only.** The verified reply is vanilla hermes, not email-assistant-aware — that's #44's job.
- **Sign-off box closed via a one-account inversion test instead of a second Telegram account:** allowlist temporarily pointed at a wrong id → message from Jenit's real account produced no reply → allowlist restored → reply came back.

**How it was verified:** `hermes gateway status` confirmed `active`/`enabled` with a stable PID (no crash loop) after install. Jenit ran both phone-side tests from his own Telegram — round-trip (message → reply) and the inversion test (silence under a wrong allowlist id, then restored) — both passed 2026-07-14.

### #42 — T2.2 Worker Telegram notifier + P1 urgent ping path
**Status:** Closed

New `src/assistant/telegram.py` (`send` — raw stdlib POST, no new deps; `notify_p1` — dedupe/burst/audit orchestration), wired into `cli.py`'s `cmd_run`: every newly-classified `P1-Urgent` this run gets pinged, collapsed into one combined message on a burst.

**Decided:**
- **Two #38 mockup lines don't survive contact with a send-only worker.** Dropped the burst's "Reply with a number to open" — the worker never receives replies (only the hermes gateway polls, #41) and no Phase-2 conversation-playbook intent resolves a bare numeric reference, so the line would promise something that goes nowhere. The single-ping mockup's draft-link line stays omitted, per #38's own grounding rule (no drafting until Phase 3).
- **Ping failures are fully isolated from run health.** A failed send is recorded as a `failed` action_event (visible via `assistant audit`) but never raises, never increments `error_count`, never flips `run_events.status`, never affects the CLI exit code — so a flaky Telegram API can't trip #38's N=5-consecutive-failure alert (reserved for actual Gmail/triage failures) or make `assistant status` call a healthy worker unhealthy.
- **Dedupe/audit reuses the existing `action_events` audit-before-write pattern exactly**, mirroring `apply.py`: one `intended` row per message before the send, `confirmed`/`failed` after — `action_type='telegram_ping'`, dedupe = "any `confirmed` row ever for this `gmail_message_id`." A burst still writes one row per message even though it's a single API call, same as label_add+archive today.
- **Secrets/config scope bullet was already satisfied** — `cfg.secrets.telegram_token`/`telegram_chat_id` have been loaded and validated since T1.1 (#7); no new work needed there.
- **A Python default-argument gotcha caught before it shipped:** `notify_p1`'s send function is looked up from the module at call time (`send_fn or send`, not `send_fn: SendFn = send`) specifically so `monkeypatch.setattr(telegram, "send", fake)` in a future `test_cli.py` test actually intercepts the call — a bound default would have silently kept hitting the real network.

**How it was verified:** automated — `tests/test_telegram.py` (9 cases: send-URL/payload/timeout construction, non-2xx and network-error handling, single/burst compose, dedupe, cross-run dedupe, failure isolation, retry-after-failure, no-op on empty hits) plus 4 new cases in `tests/test_cli.py` (P1 triggers a real send call end-to-end through `cmd_run`, non-P1 sends nothing, dry-run sends nothing even for P1, a failing send leaves the run `status='ok'` and exit code 0) — all hand-rolled fakes at the send seam, no network, mirroring `test_apply.py`'s style. Live — Jenit crafted a P1 self-email and confirmed a real Telegram ping arrived within one poll interval, 2026-07-14.

## Open / not yet started
- #5 sign-off (hermes pin v2026.7.7.2, install layout, model config — comment posted on the issue)
- #10 visual sign-off (Jenit to confirm labels look right in Gmail web + mobile)
- Phase 1 tickets #15–#17
