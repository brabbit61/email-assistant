# email-assistant

Personal Gmail triage worker + a [hermes-agent](https://github.com/nousresearch/hermes-agent) Telegram chat layer on top. The full design and every locked decision live in **[PLAN.md](PLAN.md)**; ticket-by-ticket build history in **[progress.md](progress.md)**. This file is the replication guide — follow it top to bottom on a fresh machine to end up where this repo currently is.

## Status

**Phase 1 (triage worker) — live.** Polls Gmail every 5 min, classifies into the fixed taxonomy, applies labels for real (`dry_run = false`, go-live decided on #17).

**Phase 2 (hermes chat layer) — partial.** Built and verified: the Telegram gateway bound and locked to one chat id (T2.1), real-time P1/budget/failure/OAuth pings sent directly by the worker (T2.2, T2.3), the hermes skill teaching the agent the CLI, taxonomy, guardrails, and conversation playbook (T2.4), and three scheduled digest cron jobs at 07:00/13:00/20:00 (T2.5). **Not yet built:** the `assistant correct` verb + improvement loop (T2.6), and the phase close-out (T2.7). Following this README today gets you a working two-way Telegram Q&A agent with automatic digests — not yet in-chat corrections; the skill itself says so when asked.

## Prerequisites (one-time human setup)

Steps a human has to do outside this repo before anything below works. Full sign-off record in [progress.md](progress.md) (#1–#5).

1. **Google Cloud project → Gmail API OAuth.** Create a project, enable the Gmail API, create an OAuth **desktop app** client. Consent screen: **External**, **Testing** mode, add yourself as a test user (keeps it personal-use, no Google review needed). Download the client JSON — you'll place it at `secrets/client_secret.json` below. Scope used is `gmail.modify` only (read/label/archive/draft, **not** permanent delete — the technical backing for "never delete").
2. **Anthropic API key.** Create a key, set a console-level hard monthly spend cap. This repo's `config.toml` ships with `monthly_usd_cap = 15.00` and a worker-enforced `daily_usd_soft_cap = 0.75` — set your console cap to match or edit `config.toml`.
3. **Telegram bot.** Create one via [@BotFather](https://t.me/BotFather), grab the token. Get your own numeric chat id (message [@userinfobot](https://t.me/userinfobot) — faster than the `getUpdates` API route). One bot, one allowed chat id.
4. **Install hermes-agent** (needed for Phase 2 only — skip if you only want the Phase 1 worker). Follow [hermes-agent's own install instructions](https://github.com/nousresearch/hermes-agent). This repo was built and verified against tag `v2026.7.7.2`; pin a version rather than tracking main. After install, point it at Anthropic directly: in `~/.hermes/config.yaml` set `model.provider: anthropic`, `model.default: claude-haiku-4-5-20251001`, and put your Anthropic key in `~/.hermes/.env`. Verify with `hermes chat -q "hi"`.

## Bootstrap

Requires [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Then:

```sh
uv sync          # installs Python 3.13, creates .venv, installs deps from uv.lock
uv run assistant # verify the entry point resolves
uv run ruff check
uv run pytest    # 92 passed
```

## Secrets

All credentials live in `secrets/`, gitignored as a whole directory (`/secrets/`) so new credential files are covered automatically. Copy `.env.example` to `secrets/.env` and fill it in:

| File | Holds |
|---|---|
| `secrets/.env` | `EMAIL_ANTHROPIC_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` |
| `secrets/client_secret.json` | Google OAuth desktop-app client (from prerequisite #1) |
| `secrets/token.json` | Gmail OAuth token — created by the first auth flow below |

Non-secret tunables (model ids, budget caps, digest times, poll interval) live in the committed `config.toml`.

hermes-agent keeps its **own**, separate credentials at `~/.hermes/.env`/`~/.hermes/config.yaml` — outside this repo entirely, and duplicating the same bot token under a different env var name (`TELEGRAM_BOT_TOKEN` there vs `TELEGRAM_TOKEN` here). Two independent processes, two independent secret stores, on purpose. See [deploy/hermes-gateway.md](deploy/hermes-gateway.md).

## Gmail authorization

One-time, browser-based. Requires `secrets/client_secret.json` in place:

```sh
uv run python -m assistant.gmail   # opens browser consent, writes secrets/token.json
```

Subsequent runs are non-interactive — the stored token refreshes silently. **Re-auth** (revoked token, password change) is the same command after deleting the token:

```sh
rm secrets/token.json && uv run python -m assistant.gmail
```

A permanently unrefreshable token makes the unattended worker fail loudly (records an `auth`/`error` event, exits nonzero, and — once the Telegram pieces below are wired up — sends an immediate OAuth-death ping) rather than hang.

## Taxonomy labels

Idempotent — creates the fixed taxonomy (11 categories + 3 priorities, nested under one collapsible `Assistant` label) in Gmail, or reconciles any drift. Safe to run repeatedly:

```sh
uv run python -m assistant.labels
```

## Running the worker unattended

A systemd **user** timer fires `assistant run` every 5 minutes. One idempotent script installs it — and, if it finds `hermes` on your `PATH`, also wires up everything in the next section:

```sh
./deploy/setup.sh          # uv sync, timer install, + hermes wiring if hermes is present
```

That yields a *running* timer. For it to do useful work, authorize Gmail (above) and fill `secrets/.env` first — otherwise every run fails loudly until they're present.

The worker ships in **dry-run trial mode** (`config.toml` `[triage] dry_run = true` by default): it classifies but never writes to Gmail. Watch it, spot-check with `assistant review`, then flip the gate to go live — full procedure in **[deploy/go-live.md](deploy/go-live.md)**. (This repo's own `config.toml` already has `dry_run = false` — Jenit's instance went live on #17. A fresh clone should start `true` and follow the runbook.)

```sh
journalctl --user -u assistant.service -f            # live logs (-n 50 for recent)
systemctl --user list-timers assistant.timer         # next/last fire
systemctl --user status assistant.service            # last run's result
systemctl --user stop assistant.timer                # pause / resume
systemctl --user start assistant.timer
systemctl --user disable --now assistant.timer       # stop and remove from boot
```

After changing dependencies or the units, just rerun `./deploy/setup.sh`. Missed windows (machine asleep) run **once** on wake, not once per skipped interval (`Persistent=true`).

## `assistant` CLI reference

The seam between the worker, hermes, and manual debugging — plain text on stdout, exit 0/1.

| Verb | Purpose |
|---|---|
| `assistant run [--dry-run]` | one poll → classify → apply pass (what the timer calls) |
| `assistant status` | checkpoint age, last run, health |
| `assistant costs [--month YYYY-MM]` | spend by model/purpose vs. budget cap |
| `assistant audit [--since ISO]` | chronological confirmed/failed actions + reasoning |
| `assistant review [--since ISO]` | classifier verdicts, for dry-run-trial spot-checking |
| `assistant open` | the actionable set (Action-Needed / P1 / P2), cross-checked live against Gmail |

## Hermes agent — Telegram chat layer (Phase 2)

Everything below is optional if you only want the silent triage worker. It gets you a bot you can actually talk to: "what's urgent", "why did you file X as Y", "how much have I spent this month" — see the skill's [Conversation playbook](hermes/email-assistant/SKILL.md) for the full intent list and example dialogues.

**1. Bind the gateway to your bot.** One-time config in hermes's own files (not this repo's `secrets/`), then install it as a service. Full runbook: **[deploy/hermes-gateway.md](deploy/hermes-gateway.md)**.

```sh
hermes gateway install   # writes + enables the systemd user service, starts it
hermes gateway status    # confirm active
```

**2. Wire up the skill and lock hermes down.** `./deploy/setup.sh` (rerun it — it's idempotent) does three things automatically once it detects `hermes` on `PATH`:

- **Symlinks the skill** — `hermes/email-assistant/` into `~/.hermes/skills/email/email-assistant`. The repo stays the single source of truth; edits here are live for the agent immediately, no reinstall.
- **Quiets the chat display** — sets `display.interim_assistant_messages` and `display.tool_progress` to `false` in `~/.hermes/config.yaml`. Without this, hermes narrates every tool call/command into the Telegram chat, which reads as broken, not transparent.
- **Restricts hermes to this repo's skill** — `hermes skills opt-out --remove --yes`, removing every bundled skill (himalaya, google-workspace, the creative/research/dev tool skills, etc.) so `email-assistant` is the only thing hermes can reach for.

> **Why the lockdown matters, concretely:** without it, a generic bundled email skill can get picked for an "email" question ahead of this repo's purpose-built one — and, worse, one of them will happily try to walk you through handing it a raw Gmail password to configure IMAP/SMTP access. That's a completely separate, ungated write channel with none of this project's guardrails (never send / never delete / never click links / read-only-except-two-bounded-writes). **Never give hermes your Gmail password** — the only credential it should ever need is nothing; all Gmail access flows through the worker's own OAuth token via `assistant open`/the CLI, read-only.

This is a global change to your hermes profile, not scoped to this repo — it removes skills you may have used for unrelated things. Reversible any time: `hermes skills opt-in --sync`. If the gateway was already running when you run this, pick up the change with:

```sh
hermes gateway restart
```

**3. Register the three digests.** Also part of `./deploy/setup.sh`: three hermes cron jobs (07:00 / 13:00 / 20:00, host-local time) that each tell the agent to compose and send a digest per the skill's [Digest structure](hermes/email-assistant/SKILL.md) section — cumulative standing state, a staleness warning if the checkpoint is stale, and (evening) the running monthly spend. Definitions live in [hermes/cron-jobs.md](hermes/cron-jobs.md); registration is idempotent (name-guarded) and picked up live by the gateway's cron ticker, no restart needed. Inspect or change them:

```sh
hermes cron list                      # see all three, next-run times
hermes cron run email-digest-morning  # fire one now, off-schedule
hermes cron remove <job-id>           # delete, then rerun setup.sh to recreate
```

**4. Verify.** From your Telegram account, message the bot:
- `ping` → confirms the round-trip (vanilla hermes reply)
- `what's urgent?` → should answer from `assistant open`, not narrate a command
- `delete all my newsletters` → should refuse, citing the guardrail, with a manual Gmail path

Lockdown inversion test (confirms the allowlist is fail-closed): stop the gateway, temporarily set a wrong id in `~/.hermes/.env`'s `TELEGRAM_ALLOWED_USERS`, restart, message from your real account and confirm **silence** (no reply, no pairing prompt), then restore the real id and restart again. Full steps in [deploy/hermes-gateway.md](deploy/hermes-gateway.md).

```sh
journalctl --user -u hermes-gateway -f   # gateway logs
hermes gateway status                    # service state
hermes skills list                       # should show exactly one skill: email-assistant
```

## Layout

```
src/assistant/          # the triage worker + `assistant` CLI (Phase 1)
  gmail.py                 OAuth + Gmail API client
  classify.py               Haiku structured-output classifier
  rubric.md                 taxonomy + tie-break rules (source of truth, read live by classify.py)
  poll.py                   incremental history-API poller + checkpoint
  apply.py                  label/archive applier, audit-before-write
  telegram.py                worker's own pings: P1, budget, failure, OAuth-death, recovery
  store.py                   SQLite schema + migrations, shared helpers
  cli.py                     the `assistant` subcommands
hermes/
  email-assistant/          # the hermes skill (Phase 2) — versioned here, symlinked live
    SKILL.md                  CLI reference, taxonomy, guardrails, schema notes,
                               digest structure (#37), conversation playbook (#39)
  cron-jobs.md               # the 3 digest cron job definitions (T2.5, #45)
deploy/
  setup.sh                  idempotent: venv, systemd timer, + hermes wiring + digest cron if present
  go-live.md                 Phase-1 dry-run → live runbook
  hermes-gateway.md          Telegram gateway bind + lockdown runbook
docs/
  improvement-loop.md        S2.4 design (not yet built — T2.6)
  adr/                       architecture decision records
tests/                    # pytest, hand-rolled fakes at the Gmail/Telegram seams, no network
config.toml              # non-secret tunables — committed
secrets/                 # credentials — gitignored, never committed
data/                    # runtime state (triage.db) — gitignored, created at runtime
```

## Costs

Actuals so far track the estimate in [PLAN.md](PLAN.md): ~$1–4/mo on Haiku for ongoing classification at personal mail volume, a few dollars more once digests/chat are in regular use on Sonnet. `assistant costs` shows the real breakdown at any time.

## Migrating to another machine

Clone the repo, `uv sync`, copy `secrets/` and `data/` over, run `./deploy/setup.sh`. For the hermes layer, install hermes-agent fresh on the new machine (its profile under `~/.hermes/` isn't repo state) and repeat the "Hermes agent" section above — `setup.sh` re-running there does the skill link, display quieting, and lockdown the same way. Planned as Phase 5 (dedicated always-on box); not yet executed.
