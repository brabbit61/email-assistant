#!/usr/bin/env bash
# Idempotent install of the triage worker as a systemd *user* timer.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

command -v uv >/dev/null 2>&1 || {
	echo "error: 'uv' not found. Install it: https://astral.sh/uv" >&2
	exit 1
}

echo "==> syncing dependencies (.venv)"
uv sync --project "$ROOT"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

echo "==> writing systemd user units to $UNIT_DIR"
cat >"$UNIT_DIR/assistant.service" <<EOF
[Unit]
Description=Email assistant triage worker (one poll->classify->apply pass)

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment="EMAIL_ASSISTANT_HOME=$ROOT"
ExecStart="$ROOT/.venv/bin/assistant" run
EOF

cat >"$UNIT_DIR/assistant.timer" <<'EOF'
[Unit]
Description=Run the email assistant triage worker every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Linger keeps the user manager alive across logout/reboot so the timer fires
# unattended. Own-user linger needs no root on standard systemd; warn (don't abort)
# where it's restricted — the timer still works within a login session.
loginctl enable-linger "$USER" 2>/dev/null ||
	echo "warning: could not enable linger; timer won't survive logout without it"

echo "==> enabling timer"
systemctl --user daemon-reload
systemctl --user enable --now assistant.timer

[ -f "$ROOT/secrets/.env" ] ||
	echo "note: secrets/.env missing — runs fail loudly until you add it (see README)."

if command -v hermes >/dev/null 2>&1; then
	echo "==> linking hermes skill (repo stays the source of truth)"
	HERMES_SKILLS="$HOME/.hermes/skills"
	mkdir -p "$HERMES_SKILLS/email"
	ln -sfn "$ROOT/hermes/email-assistant" "$HERMES_SKILLS/email/email-assistant"
	LINKED="$(readlink -f "$HERMES_SKILLS/email/email-assistant")"
	[ "$LINKED" = "$ROOT/hermes/email-assistant" ] ||
		{ echo "error: hermes skill symlink did not resolve into the repo" >&2; exit 1; }

	# Without these, hermes narrates every tool call/command into the Telegram
	# chat (interim "thinking" messages + raw tool-progress lines) — fine in a
	# terminal, unreadable as a chatbot. Idempotent; safe to re-run.
	# Matches config.toml's [models] agent — digests/chat need a model that
	# reliably follows multi-constraint formatting/arithmetic instructions;
	# Haiku was tried and dropped a lead-in sentence + miscounted digest
	# totals on live testing (T2.5, #45).
	echo "==> setting hermes's default model to match config.toml's [models] agent"
	hermes config set model.default claude-sonnet-5 ||
		echo "warning: could not set model.default"

	echo "==> quieting hermes chat display for the Telegram UX"
	hermes config set display.interim_assistant_messages false ||
		echo "warning: could not set display.interim_assistant_messages (is hermes initialized? run 'hermes gateway install' first)"
	hermes config set display.tool_progress false ||
		echo "warning: could not set display.tool_progress"

	# Bundled skills (himalaya, google-workspace, ...) are generic tools with
	# none of this repo's guardrails. --remove strips any already-seeded ones too, not just
	# future ones; only unmodified bundled skills are touched, local/hub skills
	# are never removed. Idempotent (no-ops once opted out).
	echo "==> restricting hermes to this repo's skill only"
	hermes skills opt-out --remove --yes ||
		echo "warning: could not opt hermes out of bundled skills"
	echo "note: if the hermes gateway is already running, 'hermes gateway restart' picks up the reduced skill set."

	# Registers the 3 digest cron jobs from hermes/cron-jobs.md (T2.5, #45).
	# Grep-guarded on job name so re-running never creates duplicates; picked
	# up live by the gateway's cron ticker on its next tick, no restart needed.
	if [ -f "$ROOT/secrets/.env" ] && grep -q '^TELEGRAM_CHAT_ID=' "$ROOT/secrets/.env"; then
		CHAT_ID="$(grep '^TELEGRAM_CHAT_ID=' "$ROOT/secrets/.env" | cut -d= -f2-)"
		echo "==> registering digest cron jobs (07:00 / 13:00 / 20:00, host-local time)"

		register_digest() {
			name="$1" schedule="$2" prompt="$3"
			hermes cron list 2>/dev/null | grep -q "$name" && return 0
			hermes cron create "$schedule" "$prompt" \
				--name "$name" \
				--deliver "telegram:$CHAT_ID" \
				--skill email-assistant \
				--workdir "$ROOT" ||
				echo "warning: could not register cron job $name"
		}

		register_digest "email-digest-morning" "0 7 * * *" \
			"Compose and send Jenit's MORNING email digest now, following the Digest structure section of your email-assistant skill (morning window: overnight since 20:00). If \`assistant status\` shows the checkpoint is over 60 min stale, lead with the staleness warning. Output only the finished digest — no narration, no command output."
		register_digest "email-digest-midday" "0 13 * * *" \
			"Compose and send Jenit's MIDDAY email digest now, following the Digest structure section of your email-assistant skill (midday window: since 07:00). If \`assistant status\` shows the checkpoint is over 60 min stale, lead with the staleness warning. Output only the finished digest — no narration, no command output."
		register_digest "email-digest-evening" "0 20 * * *" \
			"Compose and send Jenit's EVENING email digest now, following the Digest structure section of your email-assistant skill (evening window: since 13:00; end with the running monthly spend line per the skill). If \`assistant status\` shows the checkpoint is over 60 min stale, lead with the staleness warning. Output only the finished digest — no narration, no command output."

		echo "note: cron times are host-local (see hermes/cron-jobs.md); pin with 'hermes config set timezone <zone>' if this system's timezone ever changes."
	else
		echo "note: secrets/.env or TELEGRAM_CHAT_ID missing — skipping digest cron registration (see README)."
	fi
else
	echo "note: hermes CLI not found on PATH — skipping hermes skill link + display config (install hermes first, then re-run)."
fi

echo
systemctl --user list-timers assistant.timer --no-pager || true
echo
echo "Done. Logs: journalctl --user -u assistant.service -f"
echo "Worker is in DRY-RUN trial mode (config.toml [triage] dry_run = true): it"
echo "classifies but never writes to Gmail. Go live per deploy/go-live.md."
