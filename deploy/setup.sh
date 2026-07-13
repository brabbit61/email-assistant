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

echo
systemctl --user list-timers assistant.timer --no-pager || true
echo
echo "Done. Logs: journalctl --user -u assistant.service -f"
