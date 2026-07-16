"""Anthropic $/1M-token rates for the two model-cost sites in this repo: the
worker's own classifier calls (classify.cost_usd) and imported hermes agent
sessions (cli._hermes_session_cost). One table, so a price change or new
model is a single edit instead of two.

ponytail: rates hardcoded; update here on an Anthropic price change or when
hermes/config.toml starts using a new model.
"""

from __future__ import annotations

PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.1,
        "cache_write": 1.25,
    },
    "claude-sonnet-5": {  # kept for hermes sessions already on record pre-haiku-switch
        "input": 2.0,
        "output": 10.0,
        "cache_read": 0.2,
        "cache_write": 2.5,
    },
}
