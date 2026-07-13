"""Gmail OAuth: first-run authorization, token persistence, silent refresh (T1.3, issue #9).

One durable credential drives the worker for months. `get_credentials()` is the
only entry point callers need:

- **Worker (unattended):** `get_credentials(config)` — non-interactive. A valid or
  refreshable token returns silently; anything that would require a human raises
  `AuthError` (loud, never a browser hang).
- **First run / re-auth (human present):** `get_credentials(config, interactive=True)`
  opens the browser consent once and writes `secrets/token.json`. Re-auth is the
  same command after deleting that file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from assistant.config import Config, load

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class AuthError(Exception):
    """Authorization cannot proceed non-interactively — a human must re-run the
    documented auth command. Message is safe to log (no token material)."""


def _load(token_path: Path) -> Credentials | None:
    if not token_path.is_file():
        return None
    return Credentials.from_authorized_user_file(str(token_path), SCOPES)


def _persist(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)  # durable secret; secrets/ is already 0700


def _run_flow(config: Config) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.client_secret_path), SCOPES
    )
    return flow.run_local_server(port=0)  # opens browser, catches the redirect


def get_credentials(config: Config, *, interactive: bool = False) -> Credentials:
    """Return usable Gmail credentials, refreshing or (if interactive) authorizing.

    Non-interactive by default so the unattended worker fails loudly instead of
    blocking on a browser. Raises AuthError when a human is required.
    """
    token_path = config.token_path
    creds = _load(token_path)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Revoked, password change, expired refresh token: not recoverable here.
            raise AuthError(
                f"Gmail token refresh failed permanently: {e}. "
                "Re-run the auth command: delete secrets/token.json and "
                "`uv run python -m assistant.gmail`."
            ) from e
        _persist(creds, token_path)  # refresh may rotate the token / bump expiry
        return creds

    if not interactive:
        raise AuthError(
            "No usable Gmail token and running non-interactively. Authorize once: "
            "`uv run python -m assistant.gmail`."
        )

    creds = _run_flow(config)
    _persist(creds, token_path)
    return creds


def main() -> int:
    """Documented auth command: `uv run python -m assistant.gmail`.

    First run opens browser consent; later runs just confirm/refresh. On permanent
    failure, records a loud auth error in the DB and exits nonzero (visible via
    `assistant status`; Telegram alerting arrives in Phase 2).
    """
    config = load()
    try:
        get_credentials(config, interactive=True)
    except AuthError as e:
        _record_auth_failure(config, str(e))
        print(f"AUTH FAILED: {e}", file=sys.stderr)
        return 1
    print(f"Gmail authorized. Token stored at {config.token_path}.")
    return 0


def _record_auth_failure(config: Config, message: str) -> None:
    """Append a loud, durable failure event (append-only run_events, per T1.2)."""
    from assistant import (
        store,
    )  # local import: auth doesn't need the DB on the happy path

    conn = store.open_db(config.db_path)
    try:
        conn.execute(
            "INSERT INTO run_events(run_id, phase, status, note, recorded_at) "
            "VALUES (?, 'auth', 'error', ?, ?)",
            (store.new_id(), message, store.now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
