"""T1.8 CLI: run/status/audit/costs — dispatch, exit codes, output shape.

Fakes at the gmail/classify/poll module seams (monkeypatch), like test_poll.py.
`apply_verdict` runs for real against a hand-rolled fake Gmail service (like
test_apply.py) so the run loop's wiring is exercised end-to-end.
"""

import argparse
import sqlite3

import pytest

from assistant import classify, cli, config, gmail, poll, store, telegram
from assistant.classify import Usage, Verdict
from assistant.labels import FULL_NAME
from assistant.poll import RunResult

ALL_LABEL_NAMES = list(FULL_NAME.values())

CONFIG_TOML = """\
[models]
classifier = "claude-haiku-4-5-20251001"
agent = "claude-sonnet-5"

[budget]
monthly_usd_cap = 15.0
daily_usd_soft_cap = 0.75

[triage]
poll_interval_minutes = 5
dry_run = false

[digest]
times = ["07:00", "13:00", "20:00"]
"""

# Same, but the go-live gate still closed (dry-run trial mode).
CONFIG_TOML_GATE = CONFIG_TOML.replace("dry_run = false", "dry_run = true")


class _ListExec:
    def __init__(self, labels):
        self._labels = labels

    def execute(self):
        return {"labels": self._labels}


class _ModifyExec:
    def __init__(self, calls, msg_id, body, fail):
        self._calls, self._msg_id, self._body, self._fail = calls, msg_id, body, fail

    def execute(self):
        if self._fail:
            raise RuntimeError("gmail outage")
        self._calls.append((self._msg_id, self._body))
        return {}


class FakeService:
    def __init__(self, fail_ids: frozenset[str] = frozenset()):
        self.calls: list[tuple[str, dict]] = []
        self._labels = [{"id": f"id_{n}", "name": n} for n in ALL_LABEL_NAMES]
        self._fail_ids = fail_ids

    def users(self):
        return self

    def labels(self):
        return self

    def list(self, userId):
        return _ListExec(self._labels)

    def messages(self):
        return self

    def modify(self, userId, id, body):
        return _ModifyExec(self.calls, id, body, id in self._fail_ids)


def _make_repo(tmp_path, config_toml=CONFIG_TOML):
    (tmp_path / "config.toml").write_text(config_toml)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / ".env").write_text(
        'EMAIL_ANTHROPIC_API_KEY="sk-ant-test"\nTELEGRAM_TOKEN=000:test\nTELEGRAM_CHAT_ID=1\n'
    )
    return tmp_path


def _patch_config(monkeypatch, root):
    load = config.load  # capture before patching, else the lambda calls itself
    monkeypatch.setattr(config, "load", lambda: load(home=root))


def _seed_messages(conn, ids):
    for mid in ids:
        conn.execute(
            "INSERT INTO messages(gmail_message_id, sender, subject, body, first_seen_at) "
            "VALUES (?, 'a@b.com', ?, 'body', ?)",
            (mid, f"subj {mid}", store.now_iso()),
        )
    conn.commit()


def _patch_run(monkeypatch, root, svc, verdicts, poll_result=None):
    _patch_config(monkeypatch, root)
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())
    monkeypatch.setattr(gmail, "service", lambda creds: svc)
    monkeypatch.setattr(classify, "make_client", lambda key: object())
    monkeypatch.setattr(
        poll, "poll_once", lambda conn, svc: poll_result or RunResult(0, 0, False, "1")
    )
    calls = iter(verdicts)
    monkeypatch.setattr(classify, "classify", lambda client, model, email: next(calls))


def test_run_classifies_and_labels_new_messages(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Finance", "P2-This-Week", "autopay due"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            ),
            (
                Verdict("Newsletters", None, "weekly digest"),
                Usage("claude-haiku-4-5-20251001", 8, 4),
            ),
        ],
        poll_result=RunResult(2, 2, False, "200"),
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1", "m2"])

    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 0
    out = capsys.readouterr().out
    assert "Poll: 2 new messages" in out
    assert "Classified: 2 (0 unclassified)" in out
    assert "0 error(s)." in out
    assert len(svc.calls) == 2  # one combined modify call per message
    confirmed = conn.execute(
        "SELECT COUNT(*) FROM current_actions WHERE status='confirmed'"
    ).fetchone()[0]
    assert confirmed == 3  # Finance + P2-This-Week for m1, Newsletters for m2
    assert (
        conn.execute(
            "SELECT status FROM run_events WHERE phase='triage' AND status IS NOT NULL "
            "ORDER BY event_id DESC LIMIT 1"
        ).fetchone()["status"]
        == "ok"
    )


def test_run_pings_telegram_on_p1_and_records_confirmed_action(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Finance", "P1-Urgent", "failed autopay, bill due today"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            ),
        ],
        poll_result=RunResult(1, 1, False, "200"),
    )
    sent = []
    monkeypatch.setattr(
        telegram,
        "send",
        lambda token, chat_id, text: sent.append((token, chat_id, text)),
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])

    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 0
    assert len(sent) == 1
    token, chat_id, text = sent[0]
    assert (token, chat_id) == ("000:test", "1")  # from _make_repo's secrets/.env
    assert "failed autopay, bill due today" in text
    confirmed = conn.execute(
        "SELECT COUNT(*) FROM action_events "
        "WHERE action_type='telegram_ping' AND status='confirmed'"
    ).fetchone()[0]
    assert confirmed == 1


def test_run_sends_no_ping_for_non_p1_mail(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Newsletters", None, "weekly digest"),
                Usage("claude-haiku-4-5-20251001", 8, 4),
            ),
        ],
        poll_result=RunResult(1, 1, False, "200"),
    )
    sent = []
    monkeypatch.setattr(
        telegram,
        "send",
        lambda token, chat_id, text: sent.append((token, chat_id, text)),
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])

    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 0
    assert sent == []


def test_run_dry_run_sends_no_ping_even_for_p1(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Finance", "P1-Urgent", "wire confirmation needed"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            ),
        ],
        poll_result=RunResult(1, 1, False, "200"),
    )
    sent = []
    monkeypatch.setattr(
        telegram,
        "send",
        lambda token, chat_id, text: sent.append((token, chat_id, text)),
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])

    code = cli.cmd_run(argparse.Namespace(dry_run=True))

    assert code == 0
    assert sent == []


def test_run_ping_failure_does_not_fail_the_run(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Finance", "P1-Urgent", "urgent"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            ),
        ],
        poll_result=RunResult(1, 1, False, "200"),
    )

    def failing_send(token, chat_id, text):
        raise RuntimeError("telegram outage")

    monkeypatch.setattr(telegram, "send", failing_send)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])

    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 0  # ping failure must never fail the triage run
    out = capsys.readouterr().out
    assert "0 error(s)." in out
    failed = conn.execute(
        "SELECT COUNT(*) FROM action_events "
        "WHERE action_type='telegram_ping' AND status='failed'"
    ).fetchone()[0]
    assert failed == 1
    status = conn.execute(
        "SELECT status FROM run_events WHERE phase='triage' AND status IS NOT NULL "
        "ORDER BY event_id DESC LIMIT 1"
    ).fetchone()["status"]
    assert status == "ok"  # notification health != triage health


def test_run_dry_run_records_cost_but_writes_no_gmail_mutations(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Work", "P3-FYI", "standup notes"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            )
        ],
        poll_result=RunResult(1, 1, False, "200"),
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])

    code = cli.cmd_run(argparse.Namespace(dry_run=True))

    assert code == 0
    out = capsys.readouterr().out
    assert "Would label 1 message(s)" in out
    assert "[DRY RUN" in out
    assert svc.calls == []
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1


def test_config_gate_dries_run_without_the_flag(tmp_path, monkeypatch, capsys):
    """dry_run=true in config suppresses writes even when --dry-run is absent —
    the go-live gate the unattended timer obeys (T1.11)."""
    root = _make_repo(tmp_path, CONFIG_TOML_GATE)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Finance", None, "bill due"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            )
        ],
        poll_result=RunResult(1, 1, False, "200"),
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])

    code = cli.cmd_run(
        argparse.Namespace(dry_run=False)
    )  # no CLI flag, gate still dries

    assert code == 0
    out = capsys.readouterr().out
    assert "Would label 1 message(s)" in out
    assert "config gate" in out  # tells the operator why it's dry
    assert svc.calls == []  # nothing written to Gmail
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0
    # ...but the verdict is still recorded, so `review` can spot-check it
    assert (
        conn.execute(
            "SELECT category FROM current_classifications WHERE gmail_message_id='m1'"
        ).fetchone()["category"]
        == "Finance"
    )


# --- operational pings wired through cmd_run (T2.3, #43) ----------------------


def _capture_send(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(telegram, "send", lambda tok, chat, text: sent.append(text))
    return sent


def test_run_oauth_death_pings_and_propagates(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)

    def dead_auth(cfg):
        raise gmail.AuthError("token refresh failed permanently")

    monkeypatch.setattr(gmail, "get_credentials", dead_auth)
    sent = _capture_send(monkeypatch)

    with pytest.raises(gmail.AuthError):
        cli.cmd_run(argparse.Namespace(dry_run=False))

    assert any("auth dead" in s.lower() for s in sent)
    conn = store.open_db(root / "data" / "triage.db")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM action_events "
            "WHERE action_type='oauth_ping' AND status='confirmed'"
        ).fetchone()[0]
        == 1
    )
    # auth dies before any run_event → never feeds the failure counter
    assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0


def test_run_failure_streak_pings_after_five_crashes(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())
    monkeypatch.setattr(gmail, "service", lambda creds: object())
    monkeypatch.setattr(classify, "make_client", lambda key: object())

    def crash(conn, svc):
        raise RuntimeError("HttpError 503 (Gmail)")

    monkeypatch.setattr(poll, "poll_once", crash)
    sent = _capture_send(monkeypatch)

    for _ in range(5):
        with pytest.raises(RuntimeError):
            cli.cmd_run(argparse.Namespace(dry_run=False))

    assert len(sent) == 1  # only the 5th crash pings
    assert "5 runs in a row" in sent[0]
    conn = store.open_db(root / "data" / "triage.db")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE phase='triage' AND status='failed'"
        ).fetchone()[0]
        == 5
    )


def test_run_recovery_pings_after_the_outage_clears(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())
    monkeypatch.setattr(gmail, "service", lambda creds: FakeService())
    monkeypatch.setattr(classify, "make_client", lambda key: object())
    sent = _capture_send(monkeypatch)

    def crash(conn, svc):
        raise RuntimeError("down")

    monkeypatch.setattr(poll, "poll_once", crash)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            cli.cmd_run(argparse.Namespace(dry_run=False))

    # worker recovers on the next run
    monkeypatch.setattr(
        poll, "poll_once", lambda conn, svc: RunResult(0, 0, False, "1")
    )
    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 0
    assert any("recovered" in s.lower() for s in sent)


def test_run_pings_budget_when_daily_soft_cap_crossed(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(monkeypatch, root, svc, [], poll_result=RunResult(0, 0, False, "1"))
    sent = _capture_send(monkeypatch)
    conn = store.open_db(root / "data" / "triage.db")
    today = store.now_iso()[:10]
    conn.execute(
        "INSERT INTO llm_calls"
        "(actor, purpose, model, input_tokens, output_tokens, cost_usd, created_at) "
        "VALUES ('worker', 'classify', 'm', 1, 1, 0.90, ?)",
        (f"{today}T08:00:00Z",),
    )
    conn.commit()

    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 0
    assert any("soft cap passed" in s.lower() for s in sent)


def test_review_lists_classifications_with_reasoning_and_since_filter(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])
    conn.execute(
        "INSERT INTO classifications(gmail_message_id, category, priority, reasoning, "
        "classified_at) VALUES ('m1', 'Finance', 'P2-This-Week', 'autopay due 07/18', ?)",
        (store.now_iso(),),
    )
    conn.commit()

    code = cli.cmd_review(argparse.Namespace(since="2020-01-01T00:00:00Z"))

    assert code == 0
    out = capsys.readouterr().out
    assert "Finance/P2-This-Week" in out
    assert "autopay due 07/18" in out  # reasoning surfaced for eyeballing
    assert "subj m1" in out  # subject from the joined messages row
    assert "1 classification(s) since 2020-01-01T00:00:00Z" in out


def test_run_retries_previously_unclassified_messages(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    svc = FakeService()
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Dev", None, "PR opened"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            )
        ],
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])
    conn.execute(
        "INSERT INTO classifications(gmail_message_id, category, reasoning, classified_at) "
        "VALUES ('m1', 'UNCLASSIFIED', 'api_error', ?)",
        (store.now_iso(),),
    )
    conn.commit()

    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 0
    latest = conn.execute(
        "SELECT category FROM current_classifications WHERE gmail_message_id='m1'"
    ).fetchone()
    assert latest["category"] == "Dev"


def test_run_isolates_a_poisoned_message(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    svc = FakeService(fail_ids=frozenset({"m1"}))
    _patch_run(
        monkeypatch,
        root,
        svc,
        [
            (
                Verdict("Finance", None, "bill due"),
                Usage("claude-haiku-4-5-20251001", 10, 5),
            ),
            (Verdict("Personal", None, "hi"), Usage("claude-haiku-4-5-20251001", 8, 4)),
        ],
        poll_result=RunResult(2, 2, False, "200"),
    )
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1", "m2"])

    code = cli.cmd_run(argparse.Namespace(dry_run=False))

    assert code == 1  # unhealthy run: nonzero exit
    out = capsys.readouterr().out
    assert "1 error(s)." in out
    m2_confirmed = conn.execute(
        "SELECT status FROM current_actions WHERE gmail_message_id='m2'"
    ).fetchone()
    assert m2_confirmed["status"] == "confirmed"  # the other message still went through


def test_status_reports_unhealthy_on_unconfirmed_action(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, messages_seen, actions_taken, "
        "error_count, history_id, recorded_at) VALUES ('r1','triage','ok',1,1,0,'100',?)",
        (store.now_iso(),),
    )
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, history_id, recorded_at) "
        "VALUES ('r1','finished','ok','100',?)",
        (store.now_iso(),),
    )
    conn.execute(
        "INSERT INTO action_events(action_id, status, action_type, actor, "
        "gmail_message_id, recorded_at) VALUES ('a1','intended','label_add','worker','m1',?)",
        (store.now_iso(),),
    )
    conn.commit()

    code = cli.cmd_status(argparse.Namespace())

    assert code == 1  # unconfirmed action = unhealthy, even though the run was 'ok'


def test_status_healthy_when_clean(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    conn = store.open_db(root / "data" / "triage.db")
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, history_id, recorded_at) "
        "VALUES ('r1','finished','ok','100',?)",
        (store.now_iso(),),
    )
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, messages_seen, actions_taken, "
        "error_count, recorded_at) VALUES ('r1','triage','ok',0,0,0,?)",
        (store.now_iso(),),
    )
    conn.commit()

    code = cli.cmd_status(argparse.Namespace())

    assert code == 0
    out = capsys.readouterr().out
    assert "Checkpoint:   historyId 100" in out


def test_audit_prints_actions_with_reasoning_and_since_filter(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_messages(conn, ["m1"])
    conn.execute(
        "INSERT INTO classifications(gmail_message_id, category, reasoning, classified_at) "
        "VALUES ('m1', 'Finance', 'autopay due 07/18', ?)",
        (store.now_iso(),),
    )
    conn.execute(
        "INSERT INTO action_events(action_id, status, action_type, actor, "
        "gmail_message_id, detail, recorded_at) VALUES "
        "('a1','confirmed','label_add','worker','m1','\N{MONEY BAG} Finance',?)",
        (store.now_iso(),),
    )
    conn.commit()

    code = cli.cmd_audit(argparse.Namespace(since="2020-01-01T00:00:00Z"))

    assert code == 0
    out = capsys.readouterr().out
    assert "autopay due 07/18" in out
    assert (
        "1 action(s) since 2020-01-01T00:00:00Z (1 confirmed, 0 failed, 0 pending)"
        in out
    )


def test_costs_aggregates_by_purpose_and_model(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    conn = store.open_db(root / "data" / "triage.db")
    month = "2026-07"
    conn.execute(
        "INSERT INTO llm_calls(actor, purpose, model, input_tokens, output_tokens, "
        "cost_usd, created_at) VALUES ('worker','classify','claude-haiku-4-5-20251001',"
        "1000,500,0.0035,?)",
        (f"{month}-05T10:00:00Z",),
    )
    conn.commit()

    code = cli.cmd_costs(argparse.Namespace(month=month))

    assert code == 0
    out = capsys.readouterr().out
    assert "2026-07 — $0.0035 of $15.0000 monthly cap" in out  # 4dp: sub-cent visible
    assert "classify" in out and "claude-haiku-4-5-20251001" in out


def _make_hermes_db(tmp_path, rows):
    """A minimal stand-in for hermes's own ~/.hermes/state.db `sessions` table —
    only the columns cmd_import_hermes reads."""
    path = tmp_path / "hermes_state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
        "cache_write_tokens INTEGER, billing_provider TEXT, ended_at REAL)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_import_hermes_prices_finished_sessions_and_skips_the_rest(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    store.open_db(root / "data" / "triage.db")

    hermes_db = _make_hermes_db(
        tmp_path,
        [
            # priced, finished, anthropic -> imported
            (
                "s1",
                "telegram",
                "claude-sonnet-5",
                1000,
                500,
                2000,
                100,
                "anthropic",
                100.0,
            ),
            # unpriced model -> skipped
            ("s2", "tui", "some-future-model", 10, 10, 0, 0, "anthropic", 200.0),
            # still open -> skipped
            ("s3", "telegram", "claude-sonnet-5", 10, 10, 0, 0, "anthropic", None),
            # not billed via anthropic -> skipped
            ("s4", "telegram", "claude-sonnet-5", 10, 10, 0, 0, "", 300.0),
        ],
    )

    code = cli.cmd_import_hermes(argparse.Namespace(hermes_db=str(hermes_db)))

    assert code == 0
    out = capsys.readouterr().out
    assert "Imported 1 hermes session(s)" in out
    assert "some-future-model" in out

    conn = store.open_db(root / "data" / "triage.db")
    row = conn.execute(
        "SELECT actor, purpose, model, cost_usd, hermes_session_id FROM llm_calls"
    ).fetchone()
    assert row["actor"] == "hermes"
    assert row["purpose"] == "telegram"
    assert row["hermes_session_id"] == "s1"
    assert row["cost_usd"] == pytest.approx(
        (1000 * 2.0 + 500 * 10.0 + 2000 * 0.2 + 100 * 2.5) / 1_000_000
    )

    # Re-running must not double-count (natural-key dedupe on hermes_session_id).
    cli.cmd_import_hermes(argparse.Namespace(hermes_db=str(hermes_db)))
    count = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    assert count == 1


# --- open: Gmail-verified still-open (T2.4, #44) ------------------------------


class FakeThreadsService:
    """svc.users().threads().get(...) fake — threads keyed by thread_id, each a
    list of {"id": ..., "labelIds": [...]}."""

    def __init__(self, threads: dict[str, list[dict]]):
        self._threads = threads
        self.calls: list[str] = []

    def users(self):
        return self

    def threads(self):
        return self

    def get(self, userId, id, format):
        self.calls.append(id)
        return self._Exec(self._threads[id])

    class _Exec:
        def __init__(self, messages):
            self._messages = messages

        def execute(self):
            return {"messages": self._messages}


def _seed_actionable(conn, mid, category, priority, thread_id="t1", reasoning="r"):
    conn.execute(
        "INSERT INTO messages(gmail_message_id, sender, subject, thread_id, "
        "first_seen_at) VALUES (?, 'a@b.com', 'subj', ?, ?)",
        (mid, thread_id, store.now_iso()),
    )
    conn.execute(
        "INSERT INTO classifications(gmail_message_id, category, priority, "
        "reasoning, classified_at) VALUES (?, ?, ?, ?, ?)",
        (mid, category, priority, reasoning, store.now_iso()),
    )
    conn.commit()


def test_open_reports_unread_inbox_unreplied_as_open(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    svc = FakeThreadsService({"t1": [{"id": "m1", "labelIds": ["UNREAD", "INBOX"]}]})
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())
    monkeypatch.setattr(gmail, "service", lambda creds: svc)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_actionable(conn, "m1", "Finance", "P1-Urgent")

    code = cli.cmd_open(argparse.Namespace())

    assert code == 0
    out = capsys.readouterr().out
    assert "Open — Gmail-verified (1):" in out
    assert "P1-Urgent" in out
    assert "1 actionable in DB · 1 open · 0 cleared" in out


def test_open_reports_read_as_cleared(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    svc = FakeThreadsService({"t1": [{"id": "m1", "labelIds": ["INBOX"]}]})  # no UNREAD
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())
    monkeypatch.setattr(gmail, "service", lambda creds: svc)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_actionable(conn, "m1", "Action-Needed", None)

    code = cli.cmd_open(argparse.Namespace())

    out = capsys.readouterr().out
    assert code == 0
    assert "Cleared since triage (1):" in out
    assert "— read" in out
    assert "1 actionable in DB · 0 open · 1 cleared" in out


def test_open_reports_archived_as_cleared(tmp_path, monkeypatch, capsys):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    svc = FakeThreadsService({"t1": [{"id": "m1", "labelIds": ["UNREAD"]}]})  # no INBOX
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())
    monkeypatch.setattr(gmail, "service", lambda creds: svc)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_actionable(conn, "m1", "Action-Needed", None)

    code = cli.cmd_open(argparse.Namespace())

    out = capsys.readouterr().out
    assert code == 0
    assert "— archived" in out


def test_open_reports_replied_as_cleared_even_if_unread_and_in_inbox(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    svc = FakeThreadsService(
        {
            "t1": [
                {"id": "m1", "labelIds": ["UNREAD", "INBOX"]},
                {"id": "m1-reply", "labelIds": ["SENT"]},
            ]
        }
    )
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())
    monkeypatch.setattr(gmail, "service", lambda creds: svc)
    conn = store.open_db(root / "data" / "triage.db")
    _seed_actionable(conn, "m1", "Action-Needed", None)

    code = cli.cmd_open(argparse.Namespace())

    out = capsys.readouterr().out
    assert code == 0
    assert "— replied" in out


def test_open_with_empty_actionable_set_never_calls_gmail(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)

    def boom(cfg):
        raise AssertionError("must not touch Gmail with nothing actionable")

    monkeypatch.setattr(gmail, "get_credentials", boom)
    store.open_db(root / "data" / "triage.db")

    code = cli.cmd_open(argparse.Namespace())

    assert code == 0
    assert "0 actionable in DB · 0 open · 0 cleared" in capsys.readouterr().out


def test_open_prints_nothing_and_fails_loudly_on_gmail_error(
    tmp_path, monkeypatch, capsys
):
    root = _make_repo(tmp_path)
    _patch_config(monkeypatch, root)
    monkeypatch.setattr(gmail, "get_credentials", lambda cfg: object())

    class DeadService:
        def users(self):
            return self

        def threads(self):
            return self

        def get(self, userId, id, format):
            raise RuntimeError("Gmail outage")

    monkeypatch.setattr(gmail, "service", lambda creds: DeadService())
    conn = store.open_db(root / "data" / "triage.db")
    _seed_actionable(conn, "m1", "Finance", "P1-Urgent")

    code = cli.cmd_open(argparse.Namespace())

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # never a partial/unverified list
    assert "Gmail check failed" in captured.err


def test_no_subcommand_prints_help_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["assistant"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()
