# Digest cron job

One hermes cron job that triggers the agent to compose and send the daily
digest per the [skill's Digest structure section](email-assistant/SKILL.md).
This file is the source of truth; `deploy/setup.sh` registers this job
automatically (idempotent — re-running never duplicates it).

Registration is a plain `hermes cron create` call — no restart needed, the
gateway's cron ticker picks up `~/.hermes/cron/jobs.json` live on its next
tick.

## Timezone

Host-local (hermes's default — no `timezone:` set in `~/.hermes/config.yaml`
and no `HERMES_TIMEZONE` env var). Time below fires in whatever wall clock
the machine is on. If this ever moves to a box in another zone, either the
box carries the right zone or run `hermes config set timezone <zone>` to pin
it explicitly.

## The job

| Name | Schedule | Window | Notes |
|---|---|---|---|
| `email-digest-evening` | `0 20 * * *` (20:00) | since yesterday 20:00 | staleness lead if checkpoint > 60 min old; ends with running monthly spend |

Prior to 2026-08-17 this ran three times a day (07:00 / 13:00 / 20:00);
morning and midday were dropped so only one, full-day digest goes out —
see `hermes/email-assistant/SKILL.md`'s Digest structure section for the
widened window.

This file is the single source for the digest schedule; `deploy/setup.sh`
registers exactly this time.

The job is pinned with `--skill email-assistant` (composition never depends
on routing) and `--workdir <repo root>` (so `uv run assistant open/status/costs`
resolve against this repo). The prompt is deliberately thin — it names the
window and defers everything else to the skill, so the improvement loop
only ever has to edit the skill file, not this job.

## Registration command

Run from the repo root, with `TELEGRAM_CHAT_ID` from `secrets/.env`:

```sh
hermes cron create "0 20 * * *" \
  "Compose and send the daily email digest now, following the Digest structure section of your email-assistant skill (window: since yesterday 20:00; end with the running monthly spend line per the skill). If \`assistant status\` shows the checkpoint is over 60 min stale, lead with the staleness warning. Output only the finished digest — no narration, no command output." \
  --name email-digest-evening \
  --deliver "telegram:$TELEGRAM_CHAT_ID" \
  --skill email-assistant \
  --workdir "$ROOT"
```

`deploy/setup.sh` runs this automatically (guarded by `hermes cron list` so
re-running never creates a duplicate).

## Inspecting / changing the job

```sh
hermes cron list                      # see it, next-run time
hermes cron run email-digest-evening  # fire it now, off-schedule
hermes cron remove <job-id>           # delete it (then rerun setup.sh to recreate it)
```

To change the job's schedule or prompt: edit this file, `hermes cron remove
<job-id>` the old one, then rerun `./deploy/setup.sh`.
