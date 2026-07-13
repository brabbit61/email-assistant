"""T1.8 CLI: run/status/audit/costs — dispatch, exit codes, output shape.

Fakes at the gmail/classify/poll module seams (monkeypatch), like test_poll.py.
`apply_verdict` runs for real against a hand-rolled fake Gmail service (like
test_apply.py) so the run loop's wiring is exercised end-to-end.
"""

import argparse

import pytest

from assistant import classify, cli, config, gmail, poll, store
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

[digest]
times = ["07:00", "13:00", "20:00"]
"""


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


def _make_repo(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG_TOML)
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
        "('a1','confirmed','label_add','worker','m1','Assistant/\N{MONEY BAG} Finance',?)",
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


def test_no_subcommand_prints_help_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["assistant"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()
