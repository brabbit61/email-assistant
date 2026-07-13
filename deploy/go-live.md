# Go-live runbook (T1.11, issue #17)

The final Phase-1 gate. The triage worker ships **off** — it classifies every new
email but writes nothing to Gmail — so you can watch it for a few days and only
then decide to let it label for real.

The gate is one line in [`config.toml`](../config.toml):

```toml
[triage]
dry_run = true   # classify only, never touch Gmail
```

`assistant run` (and the systemd timer) read this on every run. `--dry-run` on the
CLI force-dries a single manual run regardless.

## 1. Start the trial

```sh
./deploy/setup.sh          # installs + enables the 5-min timer, already in dry-run
```

Prereqs first (see the README): Gmail authorized, `secrets/.env` filled. The timer
now fires every 5 minutes, classifying new mail into the taxonomy and recording each
verdict in `data/triage.db` — without applying a single label.

Let it run **2–3 days** so it sees a representative slice of your inbox.

## 2. Spot-check accuracy

`audit` is empty during the trial (a dry run writes no action rows on purpose), so use
`review` — it lists the classifier's verdicts with the reasoning behind each:

```sh
uv run assistant review --since 2026-07-13    # verdicts since a date
uv run assistant status                       # checkpoint age, last run, errors
uv run assistant costs                        # spend vs the monthly cap
```

Read down the list and ask: **is each email labeled the way you would have labeled
it?** Acceptance bar (from PLAN.md): **≥95% sensible**, and **zero crashes** across the
trial (`status` stays healthy, error count 0).

## 3. Go live (requires sign-off)

Only after the trial clears the bar. Flip the gate:

```toml
[triage]
dry_run = false
```

That's the whole switch — the running timer picks it up on its next tick (no
`systemctl` needed). Within ~5 minutes the next batch of new mail gets real labels.
Confirm: send yourself a test email, wait one tick, check the label appears in Gmail
and `assistant audit` shows a `confirmed` row for it.

## Rollback

Set `dry_run = true` again. The next tick stops writing immediately; already-applied
labels are harmless and reversible by hand in Gmail. Nothing is ever deleted.

## Sign-off checklist

- [ ] Trial ran ≥2 days on the timer with no crashes (`status` healthy throughout)
- [ ] `assistant review` spot-check: ≥95% of new mail labeled sensibly
- [ ] `assistant costs` within expectations (~$1–4/mo ongoing)
- [ ] **Go-live decision (Jenit):** flip `dry_run = false`
