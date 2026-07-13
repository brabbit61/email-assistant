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

## Layout

```
src/assistant/   # the triage worker + `assistant` CLI (Phase 1)
hermes/          # hermes skill + cron definitions (Phase 2)
deploy/          # setup.sh + systemd units (Phase 1/5)
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
