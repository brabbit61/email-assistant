"""config loader: valid load, fail-loud on missing secrets, no value leaks."""

import pytest

from assistant.config import ConfigError, load

CONFIG_TOML = """\
[models]
classifier = "claude-haiku-4-5-20251001"
agent = "claude-sonnet-5"

[budget]
monthly_usd_cap = 15.0
daily_usd_soft_cap = 0.75

[triage]
poll_interval_minutes = 5

[digest]
times = ["07:00", "13:00", "20:00"]
"""

SECRET_TOKEN = "0000000000:SECRET-TELEGRAM-TOKEN-VALUE"


def _make_repo(tmp_path, env_body):
    (tmp_path / "config.toml").write_text(CONFIG_TOML)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / ".env").write_text(env_body)
    return tmp_path


def test_load_valid(tmp_path):
    root = _make_repo(
        tmp_path,
        "# comment line\n"
        'EMAIL_ANTHROPIC_API_KEY="sk-ant-abc"\n'
        f"TELEGRAM_TOKEN={SECRET_TOKEN}\n"
        "TELEGRAM_CHAT_ID=123456\n"
        "EMAIL_LANGSMITH_API_KEY=stale-ignored\n",  # unknown keys ignored
    )
    cfg = load(home=root)
    assert cfg.classifier_model == "claude-haiku-4-5-20251001"
    assert cfg.poll_interval_minutes == 5
    assert cfg.digest_times == ("07:00", "13:00", "20:00")
    assert cfg.secrets.telegram_token == SECRET_TOKEN  # quotes/comments parsed
    assert cfg.db_path == root / "data" / "triage.db"
    assert cfg.client_secret_path == root / "secrets" / "client_secret.json"


def test_missing_secret_names_key_without_leaking_values(tmp_path):
    root = _make_repo(
        tmp_path,
        f'EMAIL_ANTHROPIC_API_KEY="sk-ant-abc"\nTELEGRAM_TOKEN={SECRET_TOKEN}\n',
    )  # TELEGRAM_CHAT_ID absent
    with pytest.raises(ConfigError) as exc:
        load(home=root)
    msg = str(exc.value)
    assert "TELEGRAM_CHAT_ID" in msg  # names the missing key
    assert SECRET_TOKEN not in msg  # never leaks a present secret's value


def test_secrets_repr_is_masked(tmp_path):
    root = _make_repo(
        tmp_path,
        'EMAIL_ANTHROPIC_API_KEY="sk-ant-abc"\n'
        f"TELEGRAM_TOKEN={SECRET_TOKEN}\n"
        "TELEGRAM_CHAT_ID=123456\n",
    )
    cfg = load(home=root)
    assert SECRET_TOKEN not in repr(cfg)  # repr(Config) includes repr(Secrets)
    assert "***" in repr(cfg.secrets)


def test_missing_config_toml_is_clear(tmp_path):
    with pytest.raises(ConfigError, match="config.toml missing or malformed"):
        load(home=tmp_path)
