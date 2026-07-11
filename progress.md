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

## Open / not yet started
- #5 sign-off (hermes pin v2026.7.7.2, install layout, model config — comment posted on the issue)
- Phase 1 tickets (#7–#17)
