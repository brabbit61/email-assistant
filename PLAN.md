# Email Assistant on Hermes-Agent — Project Plan

## Context

Jenit's Gmail inbox is an unstructured mess. Goal: a personal email assistant he uses daily for years — not a PoC. It reads every email before he does, labels it into a fixed taxonomy, archives noise, drafts replies, and talks to him over Telegram (digests 3×/day + urgent pings + full conversational control). Built on the open-source [hermes-agent](https://github.com/nousresearch/hermes-agent) (MIT, Nous Research), which already ships the Telegram gateway, cron scheduler, memory, and skills system — so this project is mostly a small deterministic triage worker plus hermes configuration, not a framework build.

## Decisions made (grilled & locked)

| Decision | Choice |
|---|---|
| LLM | Cloud API — Anthropic. Haiku 4.5 for per-email classification, Sonnet 5 for the hermes agent |
| Gmail access | Gmail API + OAuth (desktop-app flow, `gmail.modify` scope; Gmail "folders" are labels) |
| Autonomy | Auto: label/tag, archive low-value, create drafts. Never: send, delete, click links |
| Taxonomy | Fixed set: Action-Needed, Events, Finance, Bills, Travel, Orders, Work, Personal, Dev, Newsletters, Low-Value + P1/P2/P3 priority. Agent may not invent labels. Gmail labels nest under one collapsible `Assistant` parent, emoji-prefixed, colors grouped by family (#10) |
| Channel | **Telegram** (bot via BotFather — replaced WhatsApp after grilling; no Baileys ban risk, no spare number, real bot API) |
| Notifications | Digests ~8am/1pm/7pm + immediate ping for P1-Urgent; fully two-way chat |
| Architecture | **Deterministic pipeline + agent on top** (see below) |
| Backfill | Full mailbox history — resumable batch job, cost preview before running, Anthropic Batch API (50% discount) |
| Machines | Dev + run on this Linux desktop; deploy to dedicated Windows box via **WSL2** (identical Linux path, one-time Task Scheduler autostart + never-sleep config) |
| Stretch (post-MVP) | Follow-up tracker, morning briefing + Google Calendar, subscription auditor, security sentinel |

## Architecture

```
Gmail ──poll (systemd timer, 5 min)──▶ triage worker ──▶ labels / archive / drafts
                                          │
                                          ▼
                                   SQLite (triage.db)
                                          ▲
hermes agent (Sonnet 5) ──────────────────┘  reads via `assistant` CLI (hermes skill)
   │  cron: 8am/1pm/7pm digests
   ▼
Telegram bot ◀──▶ Jenit          (urgent P1 pings sent by worker directly via bot sendMessage —
                                  alerts never depend on the agent being healthy)
```

- **Triage worker** — boring Python, no agent loop. Poll Gmail history API → for each new message: one Haiku call with structured output (fixed taxonomy) → apply label(s) via Gmail API → archive if Low-Value → record everything in SQLite. Idempotent, checkpointed (`historyId`), fails loudly. A sleeping machine just catches up on wake.
- **Hermes agent** — everything conversational. Installed with Telegram gateway; hermes-native cron jobs produce the three digests by running `assistant digest` and composing a readable summary; replies on Telegram handle "draft a polite no", "what did the bank want", "archive the newsletters". A hermes **skill** file teaches it the CLI, the taxonomy, and the guardrails (draft-never-send).
- **`assistant` CLI** — the seam between the two. Hermes executes terminal commands natively, so we expose subcommands instead of building an MCP server: `run` (one poll+triage pass), `digest --since`, `pending`, `create-draft <thread> --body-file`, `backfill`, `status`. One interface serves hermes, cron, and manual debugging.

## Repo layout (this repo)

```
pyproject.toml
src/assistant/
  gmail.py        # OAuth + Gmail API client (labels, history, archive, drafts)
  classify.py     # Haiku structured-output classifier, fixed taxonomy
  store.py        # SQLite: emails, classifications, checkpoints, alerts sent
  telegram.py     # one function: sendMessage for urgent pings
  cli.py          # the `assistant` subcommands
  backfill.py     # resumable full-history job via Anthropic Batch API, cost preview first
hermes/
  skill-email-assistant.md   # hermes skill: CLI usage, taxonomy, guardrails
  cron-jobs.md               # the 3 digest job definitions (natural language)
deploy/
  setup.sh                   # idempotent: venv, deps, generates+installs the systemd user
                             #   timer/service (5-min triage) — same script on desktop & WSL2
  windows-box.md             # WSL2 install, Task Scheduler autostart, disable sleep
```

## Phases

**Phase 0 — Prerequisites (Jenit's homework, ~1 hr)**
1. Google Cloud project → enable Gmail API → OAuth desktop-app credentials (`credentials.json`).
2. Anthropic API key.
3. Telegram: create bot with @BotFather → token; get own chat id.
4. Install hermes-agent on the Linux desktop (one-command install per its docs), pin the version.

**Phase 1 — Triage worker MVP (label-only)**
- `gmail.py`, `classify.py`, `store.py`, `cli.py run` with `--dry-run` flag (logs intended labels without applying).
- Create the 8 taxonomy labels + 3 priority labels in Gmail.
- systemd user timer every 5 min. Run 2–3 days in dry-run, then live label-only.
- Exit criteria: ≥95% of new mail labeled sensibly (spot-check), zero crashes for 3 days.

**Phase 2 — Telegram + hermes**
- Hermes gateway with Telegram; register the skill; three cron digest jobs; worker sends P1 pings directly.
- Two-way chat working: "what's urgent", "summarize X".
- Exit criteria: digests arrive on schedule for a week; urgent test email pings within ~5 min.

**Phase 3 — Actions + backfill**
- Enable auto-archive of Low-Value; `create-draft` for Action-Needed replies requested via chat.
- Backfill: count mailbox (`users.getProfile` messagesTotal), print cost estimate, confirm, then run checkpointed Batch API job overnight. Labels only — never archives historical mail.

**Phase 4 — Stretch features (each is a small addition on the SQLite log)**
- Follow-up tracker: threads awaiting replies (either direction) surfaced in digests.
- Morning briefing: add Calendar OAuth scope; 8am digest fuses meetings + RSVP-pending invites.
- Subscription auditor: monthly per-sender engagement report with unsubscribe links for Jenit to click.
- Security sentinel: phishing/spoof heuristics + classifier flag → immediate ping with explanation.

**Phase 5 — Deploy to the Windows box**
- WSL2 Ubuntu, run `deploy/setup.sh`, copy OAuth token + DB, Task Scheduler autostart, never-sleep power settings. Desktop instance becomes dev-only (worker timer disabled to avoid double-triage; SQLite checkpoint + Gmail label idempotency make an overlap harmless but pointless).

## Ticketing (immediate next action)

Tracker: **GitHub issues + milestones** in `brabbit61/email-assistant` (no Projects board — token lacks `project` scope and solo dev doesn't need one). One phase at a time; this plan creates the Phase 0 + Phase 1 batch. Phase 2 tickets are drafted only after this batch is reviewed — Phase 2 will lead with **spec tickets** (digest message mockups, urgent-ping format, conversation playbook, agent-improvement loop) that require Jenit's sign-off before implementation tickets unblock, directly answering his "what messages will I get / how does the agent improve" question.

**Setup to create first:** milestones `Phase 0: Prerequisites` … `Phase 5: Windows box deploy`; labels `type:setup|infra|feature|spec|docs`, `needs-signoff`, `user-task`, `blocked`.

**Status (2026-07-11):** milestones, labels, and the first batch are live — T0.1–T0.6 = issues #1–#6, T1.1–T1.11 = issues #7–#17. Phase 2–5 requirements are preserved as epic issues #18–#21 (one per phase, milestoned, chained by Blocked-by); each epic is broken into atomic spec-first tickets when its predecessor phase nears completion. Future sessions: read this file + the open epics to resume.

hermes-agent installed on the Linux desktop, pinned to tag **v2026.7.7.2** (commit `9de9c25`), install layout: default (`~/.hermes` config/data, `~/.local/bin/hermes` launcher). Provider configured for direct Anthropic API (`model.provider: anthropic`, `model.default: claude-sonnet-5`), verified with a single-query chat. See #5 for sign-off.

**Ticket template (every issue body, no code snippets anywhere):** Goal · Context · Scope (in/out) · Acceptance criteria · Decisions requiring sign-off (checklist Jenit ticks) · Blocked by (#refs).

### Phase 0 tickets (milestone: Phase 0)

| # | Title | Depends on | Sign-offs required |
|---|---|---|---|
| T0.1 | Tracking setup: labels, milestones, ticket conventions doc | — | conventions |
| T0.2 | Google Cloud project + Gmail API OAuth desktop credentials (`user-task`, step-by-step guide in body) | — | consent-screen type (external + self as test user); scope limited to `gmail.modify` |
| T0.3 | Anthropic API key + monthly budget cap (`user-task`) | — | budget cap value (recommend $15/mo) |
| T0.4 | Create Telegram bot via BotFather + capture own chat id (`user-task`) | — | bot name/handle |
| T0.5 | Install hermes-agent on Linux desktop, pin version | — | pinned version; install layout |
| T0.6 | Repo scaffolding + tech-stack sign-off | — | Python version (rec 3.12), env/deps manager (rec uv), config format (rec TOML + .env for secrets), lint (rec ruff) |

### Phase 1 tickets (milestone: Phase 1)

| # | Title | Depends on | Sign-offs required |
|---|---|---|---|
| T1.1 | Config & secrets loading | T0.6 | — |
| T1.2 | SQLite schema: messages, action audit log, cost ledger, checkpoints | T0.6 | schema design doc in ticket |
| T1.3 | Gmail OAuth flow + token persistence/refresh | T0.2, T1.1 | — |
| T1.4 | Create taxonomy labels in Gmail (idempotent) | T1.3 | exact label names, emoji/colors, nesting style |
| T1.5 | Incremental poller: Gmail history API + checkpoint/catch-up | T1.2, T1.3 | — |
| T1.6 | Classifier: fixed-taxonomy structured output on Haiku, token+cost recording per call | T1.1, T1.2 | classification rubric (what qualifies as P1-Urgent, Action-Needed vs Personal, etc.) |
| T1.7 | Label applier with audit-before-write guarantee | T1.4, T1.5, T1.6 | — |
| T1.8 | `assistant` CLI: `run --dry-run`, `status`, `audit`, `costs` | T1.2–T1.7 | CLI output formats |
| T1.9 | Classifier test fixtures (representative real-ish emails per category) | T1.6 | — |
| T1.10 | systemd user timer + idempotent `setup.sh` | T1.8 | — |
| T1.11 | Dry-run trial (2–3 days) + accuracy spot-check + go-live decision | T1.10 | go-live |

Dependency shape: Phase 0 is fully parallel; Phase 1 forks after T0.6 into schema/config → (Gmail chain: T1.3→T1.4/T1.5) + (classifier chain: T1.6→T1.9), converging at T1.7→T1.8→T1.10→T1.11.

## Auditability, transparency & cost awareness (cross-cutting requirement)

SQLite is the single source of truth; every artifact below is generated from it.

- **Action audit log**: every mutation the system makes — label applied/removed, archive, draft created, Telegram message sent — is a row with timestamp, gmail message id, actor (`worker`/`agent`/`backfill`), and the classifier's reasoning snippet. Nothing touches Gmail without a corresponding row (written before the API call, marked confirmed after).
- **Cost ledger**: every LLM call records model, input/output tokens, and computed USD cost (the Anthropic API returns usage on every response). Applies to worker, backfill, and hermes-driven calls alike.
- **CLI artifacts**: `assistant audit [--since]` (what was done and why), `assistant costs [--month]` (spend by model/component), `assistant status` (checkpoint, last run, error count).
- **HTML report**: `assistant report` renders a single self-contained HTML file from SQLite — triage volume by category, accuracy spot-check queue, cost over time. No server, just open the file. Regenerated weekly by a hermes cron job.
- **Cost in your face**: each evening digest ends with a one-line running monthly spend; a configurable daily budget cap triggers an immediate Telegram ping and pauses non-urgent classification if exceeded.
- **Dry-run everywhere**: `--dry-run` on `run` and `backfill` prints intended actions + estimated cost without touching Gmail or spending on labels.

Phase mapping: token/cost recording + action log + `audit`/`costs`/`status` land in **Phase 1** (foundation, not retrofit); cost line in digest + budget-cap ping in **Phase 2**; `report` HTML + weekly cron in **Phase 3**.

## Costs (estimate, shown to user before backfill)

- Ongoing: ~50–150 emails/day × ~1–2k tokens on Haiku 4.5 ≈ **$1–4/mo**; digests + chat on Sonnet ≈ **$3–8/mo**.
- Backfill: depends on mailbox size; e.g. 30k emails ≈ 45M input tokens ≈ ~$25 with Batch API pricing on Haiku. Job prints the real number first and requires confirmation.

## Verification

- `classify.py` ships one `test_classify.py` with a handful of fixture emails asserting taxonomy-valid structured output parsing.
- End-to-end after Phase 1: send self a test email → label appears in Gmail within 5 min → SQLite row exists.
- After Phase 2: `assistant digest` manually, then wait for a scheduled digest on Telegram; send a self-email crafted to be P1 → ping arrives.
- After Phase 3: backfill on a 1-week window first, verify labels, then unleash full history.

## Risks & mitigations

- **Hermes moves fast** (huge active repo): pin the installed version; our only coupling is the CLI-via-skill seam, so hermes upgrades can't break triage.
- **Misclassification**: label-only start builds trust; everything is reversible; SQLite log makes audits trivial.
- **Machine asleep at digest time**: worker checkpointing makes catch-up automatic; hermes cron fires on next wake — accepted until Phase 5 moves it to the always-on box.
- **Gmail API quotas**: personal volume is orders of magnitude under limits.
