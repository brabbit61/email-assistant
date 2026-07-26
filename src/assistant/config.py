"""Single source of configuration + secrets for the worker.

`config.toml` (committed tunables) and `secrets/.env` (gitignored credentials) sit at the repo root, alongside
`secrets/` and `data/`. Everything else resolves relative to that root.

Load once at startup via `load()`. Missing secrets fail loudly, naming exactly
what's absent; secret *values* never appear in errors or logs.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REQUIRED_SECRETS = ("EMAIL_ANTHROPIC_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")


class ConfigError(Exception):
    """Configuration or secrets are missing/invalid. Message names keys, never values."""


@dataclass(frozen=True)
class Secrets:
    anthropic_api_key: str
    telegram_token: str
    telegram_chat_id: str

    def __repr__(self) -> str:  # never leak secret values into logs/tracebacks
        return (
            "Secrets(anthropic_api_key=***, telegram_token=***, telegram_chat_id=***)"
        )


@dataclass(frozen=True)
class Config:
    root: Path
    client_secret_path: Path
    token_path: Path
    db_path: Path
    classifier_model: str
    reviewer_model: str
    monthly_usd_cap: float
    daily_usd_soft_cap: float
    dry_run: bool
    auto_archive_low_value: bool
    secrets: Secrets


def _find_root(home: Path | None) -> Path:
    """Repo root = the dir holding config.toml (or the shipped config.example.toml,
    so a fresh clone still resolves before you copy it). Explicit arg > env override
    > search up from CWD."""
    if home is not None:
        return Path(home)
    env = os.environ.get("EMAIL_ASSISTANT_HOME")
    if env:
        return Path(env)
    for d in (Path.cwd(), *Path.cwd().parents):
        if (d / "config.toml").is_file() or (d / "config.example.toml").is_file():
            return d
    raise ConfigError(
        "Could not locate config.toml. Run from the repo, or set EMAIL_ASSISTANT_HOME."
    )


def _parse_env(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser for a file we control. Whole-line # comments only
    (values may contain #); surrounding quotes stripped."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def load(home: Path | None = None) -> Config:
    root = _find_root(home)

    config_path = root / "config.toml"
    if not config_path.is_file():
        raise ConfigError(
            f"config.toml not found at {config_path}. "
            "Copy the template: cp config.example.toml config.toml"
        )
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        classifier = data["models"]["classifier"]
        reviewer = data["models"]["reviewer"]
        monthly_cap = float(data["budget"]["monthly_usd_cap"])
        daily_cap = float(data["budget"]["daily_usd_soft_cap"])
        dry_run = bool(data["triage"].get("dry_run", True))  # default safe (gate)
        auto_archive = bool(data["triage"].get("auto_archive_low_value", False))
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as e:
        raise ConfigError(
            f"config.toml missing or malformed ({config_path}): {e}"
        ) from e

    env_path = root / "secrets" / ".env"
    if not env_path.is_file():
        raise ConfigError(
            f"Secrets file not found: {env_path} (copy .env.example there and fill it in)."
        )
    env = _parse_env(env_path)
    missing = [k for k in REQUIRED_SECRETS if not env.get(k)]
    if missing:
        raise ConfigError(
            f"Missing required secrets in {env_path}: {', '.join(missing)}"
        )

    return Config(
        root=root,
        client_secret_path=root / "secrets" / "client_secret.json",
        token_path=root / "secrets" / "token.json",
        db_path=root / "data" / "triage.db",
        classifier_model=classifier,
        reviewer_model=reviewer,
        monthly_usd_cap=monthly_cap,
        daily_usd_soft_cap=daily_cap,
        dry_run=dry_run,
        auto_archive_low_value=auto_archive,
        secrets=Secrets(
            anthropic_api_key=env["EMAIL_ANTHROPIC_API_KEY"],
            telegram_token=env["TELEGRAM_TOKEN"],
            telegram_chat_id=env["TELEGRAM_CHAT_ID"],
        ),
    )
