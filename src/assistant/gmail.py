"""Gmail OAuth: first-run authorization, token persistence, silent refresh.

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

import base64
import json
import os
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from assistant.config import Config, load

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    # Deliberately broad, not the narrower calendar.events.owned +
    # calendar.freebusy pair — the "own events only" write boundary is
    # enforced by calendar.py's marker check, not by the OAuth grant itself.
    # See calendar.py's module docstring.
    "https://www.googleapis.com/auth/calendar.events",
]


def service(creds: Credentials) -> Resource:
    """Build the Gmail API client. Shared by every ticket that talks to Gmail."""
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def current_history_id(svc: Resource) -> str:
    """Mailbox's current historyId — the forward-only bootstrap point."""
    return svc.users().getProfile(userId="me").execute()["historyId"]


def iter_history(
    svc: Resource, start_id: str
) -> tuple[list[str], list[tuple[str, frozenset[str]]], str]:
    """New INBOX message ids since start_id, label-change events, and the latest historyId.

    Label events are `(message_id, frozenset(label ids added or removed))` — Flow A
    reads these to spot a human relabel of already-triaged mail. We drop the
    server-side `labelId="INBOX"` filter (it would hide relabels on archived mail) and
    filter `messagesAdded` to INBOX client-side instead, using the record's own labelIds.

    Drains every page. Raises HttpError(404) when start_id has aged out of Gmail's
    ~1-week history window — the caller falls back to a bounded messages.list sweep.
    """
    api = svc.users().history()
    ids: list[str] = []
    label_events: list[tuple[str, frozenset[str]]] = []
    latest = start_id
    page_token = None
    while True:
        resp = api.list(
            userId="me",
            startHistoryId=start_id,
            historyTypes=["messageAdded", "labelAdded", "labelRemoved"],
            pageToken=page_token,
        ).execute()
        latest = resp.get("historyId", latest)
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added["message"]
                if "INBOX" in msg.get("labelIds", []):
                    ids.append(msg["id"])
            for changed in record.get("labelsAdded", []) + record.get(
                "labelsRemoved", []
            ):
                label_events.append(
                    (changed["message"]["id"], frozenset(changed.get("labelIds", [])))
                )
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids, label_events, latest


def list_message_ids(svc: Resource, q: str) -> list[str]:
    """Message ids matching an arbitrary Gmail search query — the backfill scope
    query, and the bounded `in:inbox after:<epoch>` 404-recovery sweep."""
    api = svc.users().messages()
    ids: list[str] = []
    page_token = None
    while True:
        resp = api.list(userId="me", q=q, pageToken=page_token).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def message_labels(svc: Resource, msg_id: str) -> list[str]:
    """A message's current Gmail label ids, via a cheap `format='minimal'` get.
    The authoritative present state for a correction, independent of the
    possibly-stale label snapshot in a history event or the `messages` table."""
    msg = svc.users().messages().get(userId="me", id=msg_id, format="minimal").execute()
    return msg.get("labelIds", [])


def get_message(svc: Resource, msg_id: str) -> dict:
    """Full fetch → the fields the `messages` table needs, with a decoded text body."""
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    internal = msg.get("internalDate")
    return {
        "gmail_message_id": msg["id"],
        "thread_id": msg.get("threadId"),
        "sender": headers.get("from"),
        "subject": headers.get("subject"),
        "body": _decode_body(payload),
        "internal_date_ms": int(internal) if internal else None,
        "gmail_label_ids": json.dumps(msg.get("labelIds", [])),
        # ponytail: includes base64 attachment bytes in payload.parts[].body.data;
        "raw_json": json.dumps(msg),
    }


def _decode_body(payload: dict) -> str | None:
    """Walk the MIME tree; prefer text/plain, fall back to text/html. Decoded UTF-8.

    ponytail: no HTML tag-strip / is_html flag yet — add in Phase 2 if the agent's
    rendering needs it.
    """
    plain = _find_part(payload, "text/plain")
    return plain if plain is not None else _find_part(payload, "text/html")


def _find_part(payload: dict, mime: str) -> str | None:
    if payload.get("mimeType") == mime:
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        found = _find_part(part, mime)
        if found is not None:
            return found
    return None


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
    """Append a loud, durable failure event (append-only run_events)."""
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
