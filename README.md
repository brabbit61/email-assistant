# email-assistant

Personal Gmail triage worker + [hermes-agent](https://github.com/nousresearch/hermes-agent) email assistant. The full design, decisions, and phased plan live in **[PLAN.md](PLAN.md)**; ticket-by-ticket history in [progress.md](progress.md).

## Bootstrap

Requires [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Then:

```sh
uv sync          # installs Python 3.13, creates .venv, installs deps from uv.lock
uv run assistant # verify the entry point resolves
uv run ruff check
```

## Gmail authorization

One-time, browser-based. Requires `secrets/client_secret.json` (T0.2) in place:

```sh
uv run python -m assistant.gmail   # opens browser consent, writes secrets/token.json
```

Subsequent runs are non-interactive — the stored token refreshes silently. **Re-auth** (revoked token, password change) is the same command after deleting the token:

```sh
rm secrets/token.json && uv run python -m assistant.gmail
```

A permanently unrefreshable token makes the unattended worker fail loudly (records an `auth`/`error` event and exits nonzero), never hang.

## Taxonomy labels

Idempotent — creates the fixed taxonomy (11 categories + 3 priorities, nested under one collapsible `Assistant` label) in Gmail, or reconciles any drift. Safe to run repeatedly:

```sh
uv run python -m assistant.labels
```

## Running unattended

A systemd **user** timer fires `assistant run` every 5 minutes. One idempotent script installs it:

```sh
./deploy/setup.sh          # uv sync, generate + enable the timer; safe to rerun
```

That yields a *running* timer. For it to do useful work, authorize Gmail (above) and fill
`secrets/.env` first — otherwise every run fails loudly until they're present.

The worker ships in **dry-run trial mode** (`config.toml` `[triage] dry_run = true`): it
classifies but never writes to Gmail. Watch it, spot-check with `assistant review`, then
flip the gate to go live — full procedure in **[deploy/go-live.md](deploy/go-live.md)**.

```sh
journalctl --user -u assistant.service -f            # live logs (-n 50 for recent)
systemctl --user list-timers assistant.timer         # next/last fire
systemctl --user status assistant.service            # last run's result
systemctl --user stop assistant.timer                # pause / resume
systemctl --user start assistant.timer
systemctl --user disable --now assistant.timer       # stop and remove from boot
```

After changing dependencies or the units, just rerun `./deploy/setup.sh`. Missed windows
(machine asleep) run **once** on wake, not once per skipped interval (`Persistent=true`).

## Layout

```
src/assistant/   # the triage worker + `assistant` CLI (Phase 1)
hermes/          # hermes skill + cron definitions (Phase 2)
deploy/          # setup.sh — generates + installs the systemd user units (Phase 1/5)
config.toml      # non-secret tunables — committed
secrets/         # credentials — gitignored, never committed
data/            # runtime state (triage.db) — gitignored, created at runtime
```

## Secrets convention

All credentials live in `secrets/`, which is gitignored as a whole directory (`/secrets/`) so new credential files are covered automatically:

| File | Holds |
|---|---|
| `secrets/.env` | `EMAIL_ANTHROPIC_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (+ Google client id/secret) |
| `secrets/client_secret.json` | Google OAuth desktop-app client (downloaded from GCP console) |
| `secrets/token.json` | Gmail OAuth token — created by the first auth flow (T1.3) |

Non-secret tunables (model ids, budget caps, digest times, poll interval) live in the committed `config.toml`.

Migrating to another machine (Phase 5) = clone repo, `uv sync`, copy `secrets/` and `data/`.
