"""Self-checks for the classifier. The taxonomy-drift guard, the cost
math, and the two failure paths (off-taxonomy → CHECK, API error → UNCLASSIFIED)."""

import os
import sqlite3
import tempfile

from assistant import classify, store
from assistant.labels import CATEGORIES

MODEL = "claude-haiku-4-5-20251001"


def _fresh_db() -> sqlite3.Connection:
    conn = store.open_db(os.path.join(tempfile.mkdtemp(), "t.db"))
    conn.execute(
        "INSERT INTO messages(gmail_message_id, first_seen_at) VALUES ('m1', ?)",
        (store.now_iso(),),
    )
    conn.commit()
    return conn


# --- fake anthropic client -------------------------------------------------


class _Block:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _Usage:
    def __init__(self, i: int, o: int):
        self.input_tokens, self.output_tokens = i, o


class _Resp:
    def __init__(self, text: str, i: int = 10, o: int = 5):
        self.content = [_Block(text)]
        self.usage = _Usage(i, o)


class _FakeClient:
    def __init__(self, behavior):
        self._b = behavior

    class _Messages:
        def __init__(self, b):
            self._b = b

        def create(self, **kw):
            return self._b(kw)

    @property
    def messages(self):
        return _FakeClient._Messages(self._b)


# --- the CHECK constraint == labels.CATEGORIES + UNCLASSIFIED --------------


def test_check_accepts_taxonomy_and_unclassified():
    conn = _fresh_db()
    for cat in [*CATEGORIES, "UNCLASSIFIED"]:
        conn.execute(
            "INSERT INTO classifications(gmail_message_id, category, classified_at) "
            "VALUES ('m1', ?, ?)",
            (cat, store.now_iso()),
        )
    conn.commit()  # no CHECK violation => the migration list matches labels.py


def test_check_rejects_off_taxonomy():
    conn = _fresh_db()
    try:
        conn.execute(
            "INSERT INTO classifications(gmail_message_id, category, classified_at) "
            "VALUES ('m1', 'Recipts', ?)",
            (store.now_iso(),),
        )
        assert False, "expected a CHECK violation for an off-taxonomy category"
    except sqlite3.IntegrityError:
        pass


# --- cost math -------------------------------------------------------------


def test_cost_usd():
    assert abs(classify.cost_usd(MODEL, 1_000_000, 1_000_000) - 6.0) < 1e-9
    assert abs(classify.cost_usd(MODEL, 1_000_000, 1_000_000, batch=True) - 3.0) < 1e-9


# --- failure paths ---------------------------------------------------------


def test_api_error_is_unclassified():
    def boom(_kw):
        raise RuntimeError("outage")

    verdict, usage = classify.classify(
        _FakeClient(boom), MODEL, classify.Email("a", "b", "c")
    )
    assert verdict.category == classify.UNCLASSIFIED
    assert verdict.priority is None
    assert usage.input_tokens == 0


def test_malformed_response_is_unclassified():
    # Structured output can't be forced to break from a real API, so this
    # parse-failure branch is only reachable with a fake client.
    client = _FakeClient(lambda _kw: _Resp("not json at all"))
    verdict, usage = classify.classify(client, MODEL, classify.Email("a", "b", "c"))
    assert verdict.category == classify.UNCLASSIFIED
    assert verdict.priority is None
    assert "malformed_response" in verdict.reasoning
    assert usage.input_tokens == 10  # usage still recorded — unlike the API-error path


def test_off_taxonomy_persists_as_unclassified():
    conn = _fresh_db()
    client = _FakeClient(
        lambda _kw: _Resp('{"category":"Recipts","priority":"P3-FYI","reasoning":"x"}')
    )
    verdict, usage = classify.classify(client, MODEL, classify.Email("a", "b", "c"))
    assert verdict.category == "Recipts"  # classify doesn't validate; the DB does
    classify.record(conn, "m1", verdict, usage)
    row = conn.execute("SELECT category, priority FROM classifications").fetchone()
    assert row["category"] == classify.UNCLASSIFIED
    assert row["priority"] is None


def test_happy_path_records_cost():
    conn = _fresh_db()
    client = _FakeClient(
        lambda _kw: _Resp(
            '{"category":"Orders","priority":"P3-FYI","reasoning":"shipped"}'
        )
    )
    verdict, usage = classify.classify(client, MODEL, classify.Email("a", "b", "c"))
    classify.record(conn, "m1", verdict, usage)
    got = conn.execute("SELECT category, priority FROM classifications").fetchone()
    assert got["category"] == "Orders" and got["priority"] == "P3-FYI"
    assert conn.execute("SELECT cost_usd FROM llm_calls").fetchone()["cost_usd"] > 0
