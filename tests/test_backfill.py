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


def test_falls_back_to_plan_default_when_no_prior_classifications(
    tmp_path, monkeypatch
):
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

    assert (
        "Messages:     28,766 to classify  (1,234 already classified, skipped)" in out
    )
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


# --- run path (T3.5): Batch API submit / poll / ingest state machine ----------
#
# Fakes the anthropic Batch API surface (create/retrieve/results) and the two
# gmail seams (list_message_ids, get_message) against a real tmp SQLite. The
# Gmail label write is faked at backfill.apply.apply_verdict so these tests stay
# about the run loop, not the (separately tested) applier.


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _MsgUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _Message:
    def __init__(self, text, i=1000, o=50):
        self.content = [_Block(text)]
        self.usage = _MsgUsage(i, o)


class _Counts:
    def __init__(self, succeeded=0, errored=0, expired=0, canceled=0, processing=0):
        self.succeeded, self.errored, self.expired = succeeded, errored, expired
        self.canceled, self.processing = canceled, processing


class _Batch:
    def __init__(self, id, status, **counts):
        self.id, self.processing_status = id, status
        self.request_counts = _Counts(**counts)


class _Result:
    def __init__(self, type, message=None, error=None):
        self.type, self.message, self.error = type, message, error


class _Item:
    def __init__(self, custom_id, result):
        self.custom_id, self.result = custom_id, result


class _Batches:
    def __init__(self, retrieve=None, results=None):
        self.created = []
        self._retrieve, self._results = retrieve, results or []
        self._n = 0

    def create(self, requests):
        self.created.append(list(requests))
        self._n += 1
        return _Batch(f"new{self._n}", "in_progress", processing=len(requests))

    def retrieve(self, batch_id):
        return self._retrieve

    def results(self, batch_id):
        return iter(self._results)


class _Client:
    def __init__(self, batches):
        self.messages = type("M", (), {"batches": batches})()


def _fake_get_message(svc, mid):
    return {
        "gmail_message_id": mid,
        "thread_id": "t",
        "sender": "a@b.com",
        "subject": "s",
        "body": "b",
        "internal_date_ms": 1,
        "gmail_label_ids": "[]",
        "raw_json": "{}",
    }


def _seed_submitted(conn, batch_id="b1"):
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, batch_id, recorded_at) "
        "VALUES ('r1', 'backfill', 'submitted', ?, ?)",
        (batch_id, store.now_iso()),
    )
    conn.commit()


def _seed_message(conn, mid):
    conn.execute(
        "INSERT INTO messages(gmail_message_id, first_seen_at) VALUES (?, ?)",
        (mid, store.now_iso()),
    )
    conn.commit()


def test_run_submits_first_page_when_no_open_batch(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    monkeypatch.setattr(backfill.gmail, "list_message_ids", lambda svc, q: ["m1", "m2"])
    monkeypatch.setattr(backfill.gmail, "get_message", _fake_get_message)
    batches = _Batches()

    r = backfill.run(
        conn, SVC, _Client(batches), "claude-haiku-4-5-20251001", None, None
    )

    assert r.submitted_batch == "new1"
    assert r.submitted_count == 2
    assert len(batches.created[0]) == 2  # one Batch request per message
    # messages persisted up front (the classifications FK needs them), tagged
    # origin='backfill' so the live `assistant run` loop leaves them alone even
    # before (or if never) classified — see test_cli's origin-leak regression
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    origins = {
        r[0] for r in conn.execute("SELECT DISTINCT origin FROM messages").fetchall()
    }
    assert origins == {"backfill"}
    ev = conn.execute(
        "SELECT status, batch_id, messages_seen FROM run_events WHERE phase='backfill'"
    ).fetchone()
    assert (ev[0], ev[1], ev[2]) == ("submitted", "new1", 2)


def test_run_reports_still_processing_and_submits_nothing(tmp_path):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_submitted(conn, "b1")
    batches = _Batches(retrieve=_Batch("b1", "in_progress", succeeded=1, processing=1))

    r = backfill.run(conn, SVC, _Client(batches), "m", None, None)

    assert r.processing is True
    assert r.progress == (1, 2)
    assert r.submitted_batch is None  # only one batch in flight at a time
    assert batches.created == []


def test_run_ingests_ended_batch_records_labels_and_isolates(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_submitted(conn, "b1")
    for mid in ("m1", "m2", "m3"):
        _seed_message(conn, mid)
    results = [
        _Item(
            "m1",
            _Result(
                "succeeded",
                message=_Message(
                    '{"category":"Work","priority":"P1-Urgent","reasoning":"r"}'
                ),
            ),
        ),
        _Item("m2", _Result("errored")),
        _Item("m3", _Result("expired")),
    ]
    batches = _Batches(
        retrieve=_Batch("b1", "ended", succeeded=1, errored=1, expired=1),
        results=results,
    )
    labeled = []
    monkeypatch.setattr(
        backfill.apply, "apply_verdict", lambda *a, **k: labeled.append((a, k))
    )
    # after ingest the run submits the next page; empty scope -> completes cleanly
    monkeypatch.setattr(backfill.gmail, "list_message_ids", lambda svc, q: ["m1", "m2"])

    r = backfill.run(
        conn, SVC, _Client(batches), "claude-haiku-4-5-20251001", None, None
    )

    assert r.counts == (1, 1, 1, 1)  # succeeded, errored, skipped, labeled
    # succeeded -> recorded as a backfill classification, priority stripped even
    # in storage (not just the Gmail label) — the model said P1-Urgent, none of
    # that reaches classifications.priority
    assert _cls(conn, "m1") == ("Work", "backfill", None)
    # errored -> terminal UNCLASSIFIED (won't loop the backfill forever)
    assert _cls(conn, "m2") == (backfill.UNCLASSIFIED, "backfill", None)
    # expired -> no row, left for a later page to retry
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM classifications WHERE gmail_message_id='m3'"
        ).fetchone()[0]
        == 0
    )
    # label applied to m1 only, priority stripped (category-only), never archives
    assert len(labeled) == 1
    verdict = labeled[0][0][4]  # positional: conn, svc, run_id, mid, verdict
    assert verdict.category == "Work" and verdict.priority is None
    assert labeled[0][1] == {"actor": "backfill", "auto_archive": False}
    # batch closed by a page_done checkpoint, and scope now exhausted
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE phase='backfill' AND status='page_done' "
            "AND batch_id='b1'"
        ).fetchone()[0]
        == 1
    )
    assert r.complete is True


def test_ingest_is_idempotent_on_already_classified(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    _seed_submitted(conn, "b1")
    _seed_message(conn, "m1")
    conn.execute(
        "INSERT INTO classifications(gmail_message_id, category, classified_at, source) "
        "VALUES ('m1', 'Work', ?, 'backfill')",
        (store.now_iso(),),
    )
    conn.commit()
    results = [
        _Item(
            "m1",
            _Result(
                "succeeded",
                message=_Message(
                    '{"category":"Personal","priority":null,"reasoning":"r"}'
                ),
            ),
        )
    ]
    batches = _Batches(retrieve=_Batch("b1", "ended", succeeded=1), results=results)
    labeled = []
    monkeypatch.setattr(
        backfill.apply, "apply_verdict", lambda *a, **k: labeled.append(1)
    )
    monkeypatch.setattr(backfill.gmail, "list_message_ids", lambda svc, q: ["m1"])

    r = backfill.run(conn, SVC, _Client(batches), "m", None, None)

    # no duplicate row, category unchanged, no re-label
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM classifications WHERE gmail_message_id='m1'"
        ).fetchone()[0]
        == 1
    )
    assert _cls(conn, "m1") == ("Work", "backfill", None)
    assert labeled == []
    assert r.counts == (0, 0, 0, 0)  # m1 was skipped before any tally


def test_completed_batch_is_not_repolled(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    now = store.now_iso()
    for status in ("submitted", "page_done"):
        conn.execute(
            "INSERT INTO run_events(run_id, phase, status, batch_id, recorded_at) "
            "VALUES ('r1', 'backfill', ?, 'b1', ?)",
            (status, now),
        )
    conn.commit()
    monkeypatch.setattr(backfill.gmail, "list_message_ids", lambda svc, q: ["m9"])
    monkeypatch.setattr(backfill.gmail, "get_message", _fake_get_message)

    r = backfill.run(conn, SVC, _Client(_Batches()), "m", None, None)

    assert r.polled_batch is None  # b1 already ingested — goes straight to submit
    assert r.submitted_batch == "new1"


def test_run_reports_complete_when_scope_exhausted(tmp_path, monkeypatch):
    conn = store.open_db(tmp_path / "triage.db")
    monkeypatch.setattr(backfill.gmail, "list_message_ids", lambda svc, q: [])

    r = backfill.run(conn, SVC, _Client(_Batches()), "m", None, None)

    assert r.complete is True
    assert r.submitted_batch is None


def _cls(conn, mid):
    row = conn.execute(
        "SELECT category, source, priority FROM classifications WHERE gmail_message_id=?",
        (mid,),
    ).fetchone()
    return (row[0], row[1], row[2])
