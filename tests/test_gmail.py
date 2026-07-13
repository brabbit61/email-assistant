"""T1.3 Gmail auth: branch selection (valid / refresh / interactive), loud failure, 0600 token."""

import stat

import pytest
from google.auth.exceptions import RefreshError

from assistant import gmail


class FakeCreds:
    """Stand-in for google Credentials, avoiding any network/browser."""

    def __init__(
        self, *, valid=True, expired=False, refresh_token="rt", raise_on_refresh=False
    ):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._raise = raise_on_refresh
        self.refreshed = False

    def refresh(self, _request):
        if self._raise:
            raise RefreshError("revoked")
        self.refreshed = True
        self.valid = True

    def to_json(self):
        return '{"token": "fake"}'


def _config(tmp_path):
    # Only token_path/client_secret_path/db_path matter here; borrow a real Config shape.
    from assistant.config import Config, Secrets

    return Config(
        root=tmp_path,
        client_secret_path=tmp_path / "secrets" / "client_secret.json",
        token_path=tmp_path / "secrets" / "token.json",
        db_path=tmp_path / "data" / "triage.db",
        classifier_model="m",
        agent_model="m",
        monthly_usd_cap=1.0,
        daily_usd_soft_cap=1.0,
        poll_interval_minutes=5,
        digest_times=("07:00",),
        secrets=Secrets("a", "b", "c"),
    )


def test_no_token_noninteractive_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "_load", lambda p: None)
    with pytest.raises(gmail.AuthError):
        gmail.get_credentials(_config(tmp_path))  # unattended -> loud, no hang


def test_valid_token_returned_without_refresh_or_flow(tmp_path, monkeypatch):
    creds = FakeCreds(valid=True)
    monkeypatch.setattr(gmail, "_load", lambda p: creds)
    monkeypatch.setattr(gmail, "_run_flow", lambda c: pytest.fail("must not run flow"))
    assert gmail.get_credentials(_config(tmp_path)) is creds
    assert not creds.refreshed


def test_expired_token_refreshes_and_persists(tmp_path, monkeypatch):
    creds = FakeCreds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(gmail, "_load", lambda p: creds)
    cfg = _config(tmp_path)
    out = gmail.get_credentials(cfg)
    assert out is creds and creds.refreshed
    assert cfg.token_path.is_file()  # rotated token written back


def test_permanent_refresh_failure_raises_autherror(tmp_path, monkeypatch):
    creds = FakeCreds(
        valid=False, expired=True, refresh_token="rt", raise_on_refresh=True
    )
    monkeypatch.setattr(gmail, "_load", lambda p: creds)
    with pytest.raises(gmail.AuthError):
        gmail.get_credentials(_config(tmp_path))


def test_interactive_first_run_persists_0600(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "_load", lambda p: None)
    monkeypatch.setattr(gmail, "_run_flow", lambda c: FakeCreds(valid=True))
    cfg = _config(tmp_path)
    gmail.get_credentials(cfg, interactive=True)
    mode = stat.S_IMODE(cfg.token_path.stat().st_mode)
    assert mode == 0o600  # durable secret, owner-only


def test_auth_failure_recorded_in_db(tmp_path):
    cfg = _config(tmp_path)
    gmail._record_auth_failure(cfg, "boom")
    from assistant import store

    conn = store.open_db(cfg.db_path)
    row = conn.execute(
        "SELECT phase, status, note FROM run_events WHERE phase='auth'"
    ).fetchone()
    assert (row["phase"], row["status"], row["note"]) == ("auth", "error", "boom")


def _b64(text):
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode()


def test_decode_body_prefers_plain_over_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain wins")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
        ],
    }
    assert gmail._decode_body(payload) == "plain wins"


def test_decode_body_falls_back_to_html_when_no_plain():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>only html</p>")}}
        ],
    }
    assert gmail._decode_body(payload) == "<p>only html</p>"
