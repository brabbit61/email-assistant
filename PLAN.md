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
- Enable auto-archive of Low-Value (gated by an active spot-check of existing verdicts, not passive silence) + a one-time manual Gmail sweep of the pre-existing Low-Value backlog.
- Draft-reply: agent composes and places a Gmail draft immediately on request (reply-to-sender, proper In-Reply-To/References threading, Gmail-style quote-back) — no chat preview step; agent never sends.
- Backfill: `--estimate` counts mailbox (received mail only, not sent) and prints cost with zero spend; `--run --confirm` is a manual, resumable, looping command (submit batch → poll → ingest → checkpoint → next page, one Gmail-list-page per Batch API submission) that keeps going until the whole mailbox is done or it's killed — rerun the identical command to resume. Category labels only, no priority labels, `source='backfill'` excluded from the live actionable set — never archives historical mail, spend is tracked separately from the daily soft-cap. `--after`/`--before` date flags support a 1-week trial window first. Raising the Anthropic console's hard monthly spend cap before running is a sign-off item, not automated.

**Phase 4 — Stretch features (each is a small addition on the SQLite log)**
- Calendar time-blocking: chat-initiated only — for open `Action-Needed` emails, agent estimates effort (5-min intervals) at chat time and proposes a conflict-free slot; batch proposal, Jenit steers/confirms in chat, agent books real events holding email context. Third bounded agent write (create + manage own events only), amends #39's guardrails. Requires Calendar OAuth scope (`calendar.events`, extends #2's credentials).
- Full-text search: FTS5 index over the message log so ad-hoc inbox questions (existing intent 3) return fast ranked/stemmed matches over the full backfill corpus. No new CLI verb or chat intent. Semantic/embedding search is a named future upgrade, not in scope.
- ~~Follow-up tracker, subscription auditor, security sentinel~~ — dropped 2026-07-25 (Jenit: "I don't like the other items"); may resurface as new ideas later, not carried forward as-is.

**Phase 5 — Deploy to the Windows box**
- WSL2 Ubuntu, run `deploy/setup.sh`, copy OAuth token + DB, Task Scheduler autostart, never-sleep power settings. Desktop instance becomes dev-only (worker timer disabled to avoid double-triage; SQLite checkpoint + Gmail label idempotency make an overlap harmless but pointless).

## Ticketing (immediate next action)

Tracker: **GitHub issues + milestones** in `brabbit61/email-assistant` (no Projects board — token lacks `project` scope and solo dev doesn't need one). One phase at a time; this plan creates the Phase 0 + Phase 1 batch. Phase 2 tickets are drafted only after this batch is reviewed — Phase 2 will lead with **spec tickets** (digest message mockups, urgent-ping format, conversation playbook, agent-improvement loop) that require Jenit's sign-off before implementation tickets unblock, directly answering his "what messages will I get / how does the agent improve" question.

**Setup to create first:** milestones `Phase 0: Prerequisites` … `Phase 5: Windows box deploy`; labels `type:setup|infra|feature|spec|docs`, `needs-signoff`, `user-task`, `blocked`.

**Status (2026-07-11):** milestones, labels, and the first batch are live — T0.1–T0.6 = issues #1–#6, T1.1–T1.11 = issues #7–#17. Phase 2–5 requirements are preserved as epic issues #18–#21 (one per phase, milestoned, chained by Blocked-by); each epic is broken into atomic spec-first tickets when its predecessor phase nears completion. Future sessions: read this file + the open epics to resume.

**Status (2026-07-13):** Phase 1 live (`dry_run = false`, go-live recorded on #17, now closed). Phase 2 batch created from epic #18 — S2.1–S2.4 = issues #37–#40, T2.1–T2.7 = issues #41–#47 (issue numbers share the PR sequence, hence the jump). Key Phase 2 decisions, recorded in ticket bodies: agent-composed digests with the digest structure living as an evolving section of the versioned skill file; agent data seam = read-only SQLite + `assistant` CLI; budget soft-cap breach is ping-only (nothing pauses); worker pings failures it survives while the digest cron detects checkpoint staleness; one shared Telegram bot (worker send-only); improvement loop = spec + automation (proposals only, Jenit approves every change). **S2.1 (#37) decisions locked 2026-07-13:** cumulative digests at 07:00/13:00/20:00 with a Gmail-verified "still open" list — this **extends the agent seam to read-only Gmail** (ephemeral, never written back to the DB; guardrail wording flagged into #39, wiring into #44/#45). **S2.2 (#38) decisions locked 2026-07-13:** worker sends four message types — every `P1-Urgent` buzzes (per-message dedupe, once ever; burst → one combined ping; no quiet hours), daily $0.75 soft-cap breach ping (ping-only, first-per-day), failure alert at N=5 consecutive failed runs (~25 min, digest 60-min banner is the ongoing reminder), and an immediate distinct OAuth-death ping on `gmail.AuthError`. Self-contained; implemented by T2.2 (#42) and T2.3 (#43).

**S2.3 (#39) decisions locked 2026-07-14:** conversation playbook = 9 intents (attention rundown, summarize, ad-hoc question, explain-classification, status, spend, help, give-a-correction, request-improvement-review). Agent is read-only **except two bounded, Jenit-initiated writes** — apply a correction via a new `assistant correct <id> --category/--priority` verb, and open a proposal draft PR. Hard guardrails: never send / never delete / never click links / read-only-with-those-two-exceptions. Four-way refusal (forbidden / not-yet / unclear / no-data); reply composition held to Phase 3. Drafted at `docs/conversation-playbook.md`, since moved into the skill file's Conversation playbook section (T2.4, #44). **S2.4 (#40) decisions locked 2026-07-14:** on-demand improvement loop (**no cron**). Corrections captured two ways — chat → `assistant correct` (applies relabel + records a human re-classification); Gmail relabel → worker detects on poll (Flow A). Style/behavioral feedback → hermes-memory notes. Review is Jenit-triggered from chat, reads corrections since a watermark, LLM judges pattern-vs-one-off, and opens a **draft PR** (hermes gets `gh` access) editing only `rubric.md` + the skill's digest/playbook sections; taxonomy, guardrails, code, and config are immutable. Design at `docs/improvement-loop.md`. S2.4 **reopened #39** to grant the two bounded agent writes. Also fixed: `CONTEXT.md` Message definition (we store the full body, not metadata-only).

**T2.1 (#41) built and verified 2026-07-14:** hermes gateway bound to the Phase 0 Telegram bot, running as a systemd **user** service (`hermes gateway install`). Lockdown = `TELEGRAM_ALLOWED_USERS` (fail-closed allowlist env, the Telegram-specific access-control mechanism — there is no `dm_policy` for Telegram) + `unauthorized_dm_behavior: ignore` (silent deny, no pairing prompt to strangers), both in hermes's own `~/.hermes/.env`/`config.yaml`, separate from this repo's `secrets/.env`. One shared bot confirmed safe: Telegram caps `getUpdates` at one consumer per token (the gateway is it); `sendMessage` has no such cap, so the worker's send-only ping path (#42) coexists — constraint recorded on #42. Verified via round-trip (vanilla-hermes reply — email-assistant skill content is #44) and a one-account inversion test (closes the sign-off box in place of a second-account test). Runbook at `deploy/hermes-gateway.md`.

**T2.2 (#42) built and verified 2026-07-14:** new `src/assistant/telegram.py` (`send` — raw stdlib POST, no new deps; `notify_p1` — dedupe/burst/audit orchestration) wired into `cli.py`'s `cmd_run`. Two #38 mockup lines don't survive contact with a send-only worker, resolved here: the burst's "reply with a number to open" is dropped (worker never receives — only the hermes gateway polls, #41 — and no Phase-2 intent resolves a bare numeric reference); the single ping's draft-link line stays omitted per #38's own grounding rule (no drafting in Phase 2). **Ping failures are fully isolated from run health** — a failed send is a `failed` action_event, visible via `audit`, but never raises, never touches `error_count`/`run_events.status`/exit code, so a flaky Telegram API can't trip #38's N=5-failure alert or flip `assistant status` unhealthy. Dedupe/audit reuses `action_events` exactly like `apply.py` (`action_type='telegram_ping'`; dedupe = any `confirmed` row ever per `gmail_message_id`). Secrets/config scope bullet was already satisfied by T1.1 (#7). Automated coverage (`tests/test_telegram.py` + 4 cases in `tests/test_cli.py`, hand-rolled fakes, no network) proves trigger/burst/dedupe/failure-isolation; the AC's live bullet was confirmed by Jenit — a crafted P1 self-email produced a real Telegram ping within one poll interval.

**T2.3 (#43) built and verified 2026-07-14.** The three remaining S2.2 operational pings added to `telegram.py` as siblings of `notify_p1` (`notify_budget`/`notify_failure`/`notify_oauth_death`/`notify_recovery`), reusing the same `action_events` audit-before-write and failure-isolation contract (new `action_type`s, `gmail_message_id` NULL). `cmd_run` restructured into three guard points: OAuth caught narrowly around `get_credentials` (before any run_event — so an auth death writes none and never feeds the failure counter, per #38); everything else caught around the run body, which on a crash writes a `phase='triage', status='failed'` terminal (the streak signal, and it makes `assistant status` show `Last run: failed`) then alerts if the streak hit N=5; budget + recovery checks on clean completion. **Decisions beyond #38 (all signed off):** (1) *failed run* = a triage invocation with no `ok`/`error` terminal; per-message errors (`status='error'`) still count as completed, not failed. (2) **Budget day = UTC, not #38's "local calendar day"** — matches the cost ledger and `assistant costs` (no timezone exists in config; adding one just for a non-pausing nudge isn't worth the ledger/command disagreement). (3) One recovery ping (`✅ Worker recovered`) covers both a failure streak and an OAuth outage — fires on the next clean completion when the latest failure/oauth ping is newer than the latest recovery ping. (4) **All three operational pings fire regardless of `dry_run`** (unlike P1) — they report real worker state (spend, crashes, dead auth), which is just as real during the dry-run trial. Shared `store.age`/`store.clock` helpers factored out of `cli.py` for the ping timestamps. Automated coverage: ~13 new cases in `tests/test_telegram.py` (threshold/dedupe/reset/isolation for failure; over-cap/under-cap/once-per-day for budget; fire-then-silent-until-reauth + no-run-events for OAuth; unacked-alert + oauth-covered for recovery) + 4 wiring cases in `tests/test_cli.py`. The AC's three live checks were confirmed by Jenit 2026-07-14 — lower the cap → one breach ping; 5 forced crashes → failure + recovery on the next good run; revoked token → OAuth ping.

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

### Phase 2 tickets (milestone: Phase 2, created 2026-07-13)

| # | Title | Depends on | Sign-offs required |
|---|---|---|---|
| S2.1 (#37) | Spec: digest structure, grounding rules & Telegram mockups | — | send times; three digest mockups; grounding rules; staleness threshold |
| S2.2 (#38) | Spec: worker ping formats & trigger conditions (P1 / budget / failure) | — | P1 trigger + mockup; dedupe; quiet hours; breach + failure mockups and thresholds |
| S2.3 (#39) | Spec: conversation playbook | — | intent list; per-intent permissions; guardrails wording; refusal behavior |
| S2.4 (#40) | Spec: agent-improvement loop | — | capture convention; proposal surface; cadence; allowed scope |
| T2.1 (#41) | Hermes Telegram gateway: bind bot, lock to chat id, verify two-way chat | #4 (done) | lockdown verified via inversion test — done |
| T2.2 (#42) | Worker Telegram notifier + P1 urgent ping path | S2.2 | live crafted-P1-email check — done |
| T2.3 (#43) | Budget-breach + failure-alert pings in worker | S2.2, T2.2 | live breach/failure/oauth checks — done |
| T2.4 (#44) | Hermes skill file: CLI (incl. `correct`), taxonomy, guardrails, schema, digest structure, conversation playbook | S2.1, S2.3 | — |
| T2.5 (#45) | Three hermes digest cron jobs + checkpoint-staleness lead | T2.1, T2.4 | — |
| T2.6 (#46) | Improvement-loop build: `assistant correct` verb, Gmail-relabel detection (Flow A), on-demand review opening draft PRs (no cron) | S2.4, T2.4 | — |
| T2.7 (#47) | Phase 2 trial week: exit-criteria verification + close-out | all above | phase complete → close epic #18, draft Phase 3 |

Dependency shape: S2.1–S2.4 and T2.1 start in parallel → ping chain (T2.2→T2.3) and skill chain (T2.4→T2.5, T2.6) → converge at T2.7.

**Status (2026-07-22):** Phase 3 batch drafted from epic #19 ahead of #47 (Phase 2 trial week) closing — spec ticket unblocked now so sign-off can happen during the trial; every implementation ticket is `Blocked by` #47. Decisions locked during grilling, recorded in ticket bodies: archive go-live is an active spot-check of the existing Low-Value verdicts (not passive silence), followed by a one-time manual Gmail sweep of the backlog — the flag flip is code, the sweep is Jenit clicking archive in Gmail; corrections to Low-Value (chat or Gmail relabel) never auto-archive, only fresh triage does. Draft-reply places the draft immediately (no chat preview round-trip), reply-to-sender with proper threading + Gmail-style quote-back (superseded 2026-07-25 — reply-**all**, not reply-to-sender-only; see the 2026-07-25 status entry below and T3.2/#66). Backfill: `--estimate`/`--run --confirm` two-step, received mail only, one Gmail-list-page per Batch API submission is the checkpoint unit, category labels only (no priority) tagged `source='backfill'` and excluded from the live actionable set, spend fully separate from the daily soft-cap, `--run` is a manual (not timer-driven) command that loops internally until done or killed and resumes on identical re-invocation, raising the Anthropic console's hard monthly cap is a sign-off checklist item rather than code. The report HTML artifact was dropped from scope entirely (see Auditability section below).

### Phase 3 tickets (milestone: Phase 3, created 2026-07-22)

| # | Title | Depends on | Sign-offs required |
|---|---|---|---|
| S3.1 | Spec: archive go-live criteria, draft-reply mockup + guardrail wording, backfill CLI contract & cost-estimate format | — | archive go-live gate; draft-reply chat mockup; backfill `--estimate`/`--run` output format |
| T3.1 | Flip `auto_archive_low_value` + Low-Value backlog sweep | S3.1 | spot-check result; backlog swept |
| T3.2 | `create-draft` verb: compose + place Gmail draft, reply-to-sender threading | S3.1 | — |
| T3.3 | Hermes skill file: intent 10 (draft a reply), flip guardrail wording | S3.1, T3.2 | — |
| T3.4 | Backfill: mailbox count + cost estimate (`--estimate`) | S3.1 | cost estimate format |
| T3.5 | Backfill: checkpointed Batch API run (`--run --confirm`), category-only labels, source isolation | T3.4 | console spend cap raised; 1-week trial window verified |
| T3.6 | Phase 3 close-out: archive live, draft-reply verified, full backfill run | all above | full backfill go-ahead → close epic #19 |

Dependency shape: S3.1 unblocked now; every T3.x `Blocked by` #47 in addition to its S3.1/chain dependency. Archive (T3.1) and draft-reply (T3.2→T3.3) run independent of the backfill chain (T3.4→T3.5); all converge at T3.6.

**Status (2026-07-25):** Phase 3 complete — T3.1–T3.6 (#65–#70) all shipped and closed, epic #19 closed. Mid-phase correction: #64's (S3.1) locked draft-reply text said reply-to-sender-only; grilling on T3.2 (#66) flipped this to reply-all, which is what shipped — noted on #64 and corrected in the status line above. Close-out verification, real numbers pulled from `triage.db` / `assistant costs` / `assistant backfill --estimate`: **archive** — 34 confirmed auto-archives live since 2026-07-23, Jenit-spot-checked with no bad archives; **draft-reply** — one real chat-requested draft confirmed end-to-end via Telegram (2026-07-25, reply-all + threaded + quoted-back, zero failures); **backfill** — full mailbox, no date bound, complete (`--estimate` now shows 0 messages remaining to classify): 1,898 messages classified across 5 checkpointed Batch API pages, $5.80 backfill spend. Total spend this month $21.72 (145% of the $15 informational cap) — the anticipated one-time backfill cost pushing over it, an accepted overage per S3.1's sign-off (Anthropic console hard cap raised manually, not code-enforced). Next: Phase 4 stretch features (epic #20) or Phase 5 Windows deploy (epic #21) — neither drafted yet.

**Status (2026-07-25):** Phase 4 batch drafted from epic #20, grilled and trimmed to two features (follow-up tracker/subscription auditor/security sentinel dropped — Jenit didn't like them, open to new ideas later). S4.1 = issue #76, T4.1–T4.4 = issues #77–#80. **Calendar time-blocking** (S4.1/#76 decisions locked 2026-07-25): qualifying set = `Action-Needed` only, Gmail-verified still-open (#37); trigger = chat-initiated only, no digest/ping surfacing; estimate = agent reasons at chat time in 5-min intervals, no schema/rubric change; slot defaults (09:00–21:00, primary calendar, next 7 days) live in the skill file, steerable per-conversation, durable changes via hermes-memory; flow = batch proposal → chat edits → single confirm; write scope = third bounded agent write (create + manage own marker-tagged events only), amends #39's guardrails; event format = title/description with sender/subject/deadline/action/Gmail link; no Gmail-side change on scheduling (agent stays read-only there). Plumbing = new `assistant calendar` CLI verbs (T4.2/#78, same audit-before-write/dry-run contract as `apply.py`), OAuth = `calendar.events` added to #2's credentials (T4.1/#77, user-task). **Full-text search** (T4.4/#80): FTS5 over `messages`, no new verb/intent — augments existing ad-hoc-question seam; semantic/embeddings named as a future upgrade only, not built (would add a second LLM provider). Neither ticket batch implemented yet.

## Auditability, transparency & cost awareness (cross-cutting requirement)

SQLite is the single source of truth; every artifact below is generated from it.

- **Action audit log**: every mutation the system makes — label applied/removed, archive, draft created, Telegram message sent — is a row with timestamp, gmail message id, actor (`worker`/`agent`/`backfill`), and the classifier's reasoning snippet. Nothing touches Gmail without a corresponding row (written before the API call, marked confirmed after).
- **Cost ledger**: every LLM call records model, input/output tokens, and computed USD cost (the Anthropic API returns usage on every response). Applies to worker, backfill, and hermes-driven calls alike.
- **CLI artifacts**: `assistant audit [--since]` (what was done and why), `assistant costs [--month]` (spend by model/component), `assistant status` (checkpoint, last run, error count).
- **Cost in your face**: each evening digest ends with a one-line running monthly spend; a configurable daily budget cap triggers an immediate Telegram ping and pauses non-urgent classification if exceeded.
- **Dry-run everywhere**: `--dry-run` on `run` and `backfill` prints intended actions + estimated cost without touching Gmail or spending on labels.

Phase mapping: token/cost recording + action log + `audit`/`costs`/`status` land in **Phase 1** (foundation, not retrofit); cost line in digest + budget-cap ping in **Phase 2**. (The `report` HTML artifact originally planned for Phase 3 was dropped 2026-07-22 — not wanted at this stage; `status`/`audit`/`costs`/`review` already cover the same ground.)

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
