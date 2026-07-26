# Contributing

Thanks for your interest in improving email-assistant. This is a small,
opinionated project — contributions that keep it simple and well-tested are
very welcome.

## Development setup

Requires [uv](https://docs.astral.sh/uv/) (which manages Python 3.13 for you):

```sh
git clone https://github.com/brabbit61/email-assistant
cd email-assistant
uv sync                 # creates .venv, installs deps from uv.lock
cp config.example.toml config.toml
uv run assistant        # verify the entry point resolves
```

You do **not** need Gmail credentials or an Anthropic key to run the test
suite — every external seam (Gmail, Calendar, Telegram, the LLM) is faked.

## Before you open a pull request

Run the same checks CI runs — all must pass:

```sh
uv run ruff check
uv run ruff format --check
uv run pytest
```

The default `pytest` run excludes the paid, real-API classifier fixtures. They
are opt-in and cost money (real Anthropic calls); run them only if you're
changing classification behavior and have a key set:

```sh
uv run pytest -m fixtures      # needs EMAIL_ANTHROPIC_API_KEY in secrets/.env
```

## Guidelines

- **Keep the diff small.** Prefer the simplest change that works; reach for the
  standard library before a new dependency.
- **Tests are not optional for non-trivial logic.** Match the existing style:
  hand-rolled fakes at the external seam, a real temporary SQLite DB, no
  network. See any `tests/test_*.py` for the pattern.
- **Respect the guardrails.** The assistant must never gain the ability to send
  or delete email. Changes that touch the write path (`apply.py`, `draft.py`,
  `calendar.py`) get extra scrutiny.
- **Match the surrounding code.** Formatting is enforced by `ruff format`;
  import order by `ruff` (isort rules).

## Reporting bugs and requesting features

Open an issue using one of the templates. For anything security-sensitive, see
[SECURITY.md](SECURITY.md) — do not use a public issue.
