"""T3.4 backfill --estimate: scope filtering, date bounding, cost math.

Fakes at the `gmail` helper seam (monkeypatch) against a real tmp SQLite —
mirrors test_poll.py's style.
"""

from assistant import backfill, store

SVC = object()  # opaque: gmail.list_message_ids is monkeypatched


def _seed_classification(conn, mid, source="worker"):
    conn.execute(
        "INSERT INTO messages(gmail_message_id, sender, subject, body, first_seen_at) "
        "VALUES (?, 'a@b.com', 'subj', 'body', ?)",
        (mid, store.now_iso()),
    )
    conn.execute(
        "INSERT INTO classifications"
        "(gmail_message_id, category, priority, reasoning, classified_at, source) "
        "VALUES (?, 'Work', NULL, 'r', ?, ?)",
        (mid, store.now_iso(), source),
    )
    conn.commit()


def _seed_llm_call(conn, input_tokens, output_tokens):
    conn.execute(
        "INSERT INTO llm_calls(actor, purpose, model, input_tokens, output_tokens, "
        "cost_usd, created_at) VALUES ('worker', 'classify', 'claude-haiku-4-5-20251001', "
        "?, ?, 0, ?)",
        (input_tokens, output_tokens, store.now_iso()),
    )
    conn.commit()


def test_excludes_already_classified_messages(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_classification(conn, "m1", source="worker")
    _seed_classification(conn, "m2", source="human-gmail")
    monkeypatch.setattr(
        backfill.gmail, "list_message_ids", lambda svc, q: ["m1", "m2", "m3"]
    )

    result = backfill.estimate(conn, SVC, "claude-haiku-4-5-20251001", None, None)

    assert result.message_count == 1  # only m3 is unclassified
    assert result.already_classified == 2


def test_date_bounds_build_the_gmail_query(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    captured = {}

    def fake_list(svc, q):
        captured["q"] = q
        return []

    monkeypatch.setattr(backfill.gmail, "list_message_ids", fake_list)

    backfill.estimate(
        conn, SVC, "claude-haiku-4-5-20251001", "2026-07-01", "2026-07-08"
    )

    assert captured["q"] == (
        "in:inbox -in:sent -in:chats after:2026/07/01 before:2026/07/08"
    )


def test_no_date_bounds_omit_after_before(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    captured = {}
    monkeypatch.setattr(
        backfill.gmail,
        "list_message_ids",
        lambda svc, q: captured.setdefault("q", q) or [],
    )

    backfill.estimate(conn, SVC, "claude-haiku-4-5-20251001", None, None)

    assert captured["q"] == "in:inbox -in:sent -in:chats"


def test_cost_math_uses_real_average_from_llm_calls(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_llm_call(conn, 1000, 100)
    _seed_llm_call(conn, 2000, 0)  # avg: 1500 in / 50 out
    monkeypatch.setattr(backfill.gmail, "list_message_ids", lambda svc, q: ["m1", "m2"])

    result = backfill.estimate(conn, SVC, "claude-haiku-4-5-20251001", None, None)

    assert result.message_count == 2
    assert result.avg_input_tokens == 1500
    assert result.avg_output_tokens == 50
    assert result.basis_count == 2
    # cost_usd(model, 2*1500, 2*50, batch=True) computed independently below
    from assistant.classify import cost_usd

    expected = cost_usd("claude-haiku-4-5-20251001", 3000, 100, batch=True)
    assert result.est_cost_usd == expected


def test_falls_back_to_plan_default_when_no_prior_classifications(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    monkeypatch.setattr(backfill.gmail, "list_message_ids", lambda svc, q: ["m1"])

    result = backfill.estimate(conn, SVC, "claude-haiku-4-5-20251001", None, None)

    assert result.avg_input_tokens == backfill.DEFAULT_AVG_INPUT_TOKENS
    assert result.avg_output_tokens == backfill.DEFAULT_AVG_OUTPUT_TOKENS
    assert result.basis_count == 0


def test_format_output_matches_locked_spec():
    result = backfill.EstimateResult(
        message_count=28766,
        already_classified=1234,
        avg_input_tokens=1500,
        avg_output_tokens=48,
        basis_count=5412,
        est_cost_usd=21.58,
        model="claude-haiku-4-5-20251001",
        scope_label="full history (no date bound)",
    )

    out = backfill.format_estimate(result)

    assert "Messages:     28,766 to classify  (1,234 already classified, skipped)" in out
    assert "Scope:        full history (no date bound)" in out
    assert "claude-haiku-4-5-20251001, Batch API 50% discount" in out
    assert "Zero spent — estimate only." in out


def test_format_output_notes_missing_basis():
    result = backfill.EstimateResult(
        message_count=10,
        already_classified=0,
        avg_input_tokens=backfill.DEFAULT_AVG_INPUT_TOKENS,
        avg_output_tokens=backfill.DEFAULT_AVG_OUTPUT_TOKENS,
        basis_count=0,
        est_cost_usd=0.01,
        model="claude-haiku-4-5-20251001",
        scope_label="full history (no date bound)",
    )

    out = backfill.format_estimate(result)

    assert "basis: PLAN default, no prior classifications yet" in out
