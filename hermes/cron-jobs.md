# Digest cron jobs (T2.5, #45)

Three hermes cron jobs that trigger the agent to compose and send a digest
per the [skill's Digest structure section](email-assistant/SKILL.md). This
file is the source of truth; `deploy/setup.sh` registers these jobs
automatically (idempotent — re-running never duplicates them).

Registration is a plain `hermes cron create` call per job — no restart
needed, the gateway's cron ticker picks up `~/.hermes/cron/jobs.json` live on
its next tick.

## Timezone

Host-local (hermes's default — no `timezone:` set in `~/.hermes/config.yaml`
and no `HERMES_TIMEZONE` env var). Times below fire in whatever wall clock
the machine is on. If this ever moves to a box in another zone, either the
box carries the right zone or run `hermes config set timezone <zone>` to pin
it explicitly.

## The three jobs

| Name | Schedule | Window | Notes |
|---|---|---|---|
| `email-digest-morning` | `0 7 * * *` (07:00) | overnight since 20:00 | staleness lead if checkpoint > 60 min old |
| `email-digest-midday` | `0 13 * * *` (13:00) | since 07:00 | staleness lead if checkpoint > 60 min old |
| `email-digest-evening` | `0 20 * * *` (20:00) | since 13:00 | ends with running monthly spend |

Times mirror `config.toml [digest] times` — keep both in sync if you change
one.

Each job is pinned with `--skill email-assistant` (composition never depends
on routing) and `--workdir <repo root>` (so `uv run assistant open/status/costs`
resolve against this repo). Prompts are deliberately thin — they name the
slot and window and defer everything else to the skill, so the T2.6
improvement loop only ever has to edit the skill file, not these jobs.

## Registration commands

Run from the repo root, with `TELEGRAM_CHAT_ID` from `secrets/.env`:

```sh
hermes cron create "0 7 * * *" \
  "Compose and send Jenit's MORNING email digest now, following the Digest structure section of your email-assistant skill (morning window: overnight since 20:00). If \`assistant status\` shows the checkpoint is over 60 min stale, lead with the staleness warning. Output only the finished digest — no narration, no command output." \
  --name email-digest-morning \
  --deliver "telegram:$TELEGRAM_CHAT_ID" \
  --skill email-assistant \
  --workdir "$ROOT"

hermes cron create "0 13 * * *" \
  "Compose and send Jenit's MIDDAY email digest now, following the Digest structure section of your email-assistant skill (midday window: since 07:00). If \`assistant status\` shows the checkpoint is over 60 min stale, lead with the staleness warning. Output only the finished digest — no narration, no command output." \
  --name email-digest-midday \
  --deliver "telegram:$TELEGRAM_CHAT_ID" \
  --skill email-assistant \
  --workdir "$ROOT"

hermes cron create "0 20 * * *" \
  "Compose and send Jenit's EVENING email digest now, following the Digest structure section of your email-assistant skill (evening window: since 13:00; end with the running monthly spend line per the skill). If \`assistant status\` shows the checkpoint is over 60 min stale, lead with the staleness warning. Output only the finished digest — no narration, no command output." \
  --name email-digest-evening \
  --deliver "telegram:$TELEGRAM_CHAT_ID" \
  --skill email-assistant \
  --workdir "$ROOT"
```

`deploy/setup.sh` runs these automatically (guarded by `hermes cron list`
so re-running never creates duplicates).

## Inspecting / changing a job

```sh
hermes cron list                    # see all three, next-run times
hermes cron run email-digest-morning  # fire one now, off-schedule
hermes cron remove <job-id>         # delete a job (then rerun setup.sh to recreate it)
```

To change a job's schedule or prompt: edit this file, `hermes cron remove
<job-id>` the old one, then rerun `./deploy/setup.sh`.
