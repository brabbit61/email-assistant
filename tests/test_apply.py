"""T1.7 label applier: audit-before-write, dry-run, archive gating, failure isolation.

Fakes at the Gmail service seam (hand-rolled, no network) against a real tmp
SQLite via store.open_db() — mirrors test_labels.py / test_poll.py's style.
"""

import pytest

from assistant import apply, store
from assistant.classify import UNCLASSIFIED, Verdict
from assistant.labels import FULL_NAME

ALL_LABEL_NAMES = list(FULL_NAME.values())


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
    """Stand-in for svc.users().labels()/.messages(), avoiding any network."""

    def __init__(self, label_names, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self._labels = [{"id": f"id_{name}", "name": name} for name in label_names]
        self._fail = fail

    def users(self):
        return self

    def labels(self):
        return self

    def list(self, userId):
        return _ListExec(self._labels)

    def messages(self):
        return self

    def modify(self, userId, id, body):
        return _ModifyExec(self.calls, id, body, self._fail)


def _fresh_conn(tmp_path):
    conn = store.open_db(tmp_path / "triage.db")
    conn.execute(
        "INSERT INTO messages(gmail_message_id, first_seen_at) VALUES ('m1', ?)",
        (store.now_iso(),),
    )
    conn.commit()
    return conn


def test_unclassified_is_a_safe_noop(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES)
    verdict = Verdict(UNCLASSIFIED, None, "api_error")

    result = apply.apply_verdict(conn, svc, "run1", "m1", verdict)

    assert result.action_ids == [] and result.labels == []
    assert svc.calls == []
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_applies_category_and_priority_in_one_combined_call(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES)
    verdict = Verdict("Finance", "P2-This-Week", "autopay due")

    result = apply.apply_verdict(conn, svc, "run1", "m1", verdict)

    assert result.labels == [FULL_NAME["Finance"], FULL_NAME["P2-This-Week"]]
    assert len(svc.calls) == 1  # one combined modify call, not one per label
    msg_id, body = svc.calls[0]
    assert msg_id == "m1"
    assert set(body["addLabelIds"]) == {
        f"id_{FULL_NAME['Finance']}",
        f"id_{FULL_NAME['P2-This-Week']}",
    }
    assert "removeLabelIds" not in body

    current = conn.execute("SELECT status FROM current_actions").fetchall()
    assert len(current) == 2
    assert all(r["status"] == "confirmed" for r in current)
    intended = conn.execute(
        "SELECT COUNT(*) FROM action_events WHERE status='intended'"
    ).fetchone()[0]
    assert intended == 2  # audit-before-write: one intended row per confirmed one


def test_dry_run_touches_neither_gmail_nor_the_audit_log(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES)
    verdict = Verdict("Newsletters", None, "weekly digest")

    result = apply.apply_verdict(conn, svc, "run1", "m1", verdict, dry_run=True)

    assert result.labels == [FULL_NAME["Newsletters"]]
    assert result.dry_run is True
    assert svc.calls == []
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_archive_gated_by_config_and_low_value_category(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES)
    verdict = Verdict("Low-Value", None, "spam newsletter")

    result = apply.apply_verdict(conn, svc, "run1", "m1", verdict, auto_archive=True)

    assert result.archived is True
    _, body = svc.calls[0]
    assert body["removeLabelIds"] == ["INBOX"]
    archive_rows = conn.execute(
        "SELECT status FROM action_events WHERE action_type='archive' ORDER BY event_id"
    ).fetchall()
    assert [r["status"] for r in archive_rows] == ["intended", "confirmed"]


def test_archive_off_by_default_even_for_low_value(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES)
    verdict = Verdict("Low-Value", None, "spam newsletter")

    result = apply.apply_verdict(conn, svc, "run1", "m1", verdict)

    assert result.archived is False
    assert "removeLabelIds" not in svc.calls[0][1]


def test_gmail_failure_leaves_failed_terminal_events_and_reraises(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES, fail=True)
    verdict = Verdict("Work", "P3-FYI", "standup notes")

    with pytest.raises(RuntimeError, match="gmail outage"):
        apply.apply_verdict(conn, svc, "run1", "m1", verdict)

    rows = conn.execute("SELECT status FROM current_actions").fetchall()
    assert len(rows) == 2
    assert all(r["status"] == "failed" for r in rows)
    errors = conn.execute(
        "SELECT error FROM action_events WHERE status='failed'"
    ).fetchall()
    assert all("gmail outage" in r["error"] for r in errors)


def test_missing_label_raises_before_any_audit_write(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService([])  # taxonomy not reconciled: no labels exist yet
    verdict = Verdict("Dev", None, "PR opened")

    with pytest.raises(KeyError):
        apply.apply_verdict(conn, svc, "run1", "m1", verdict)

    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_relabel_adds_and_removes_in_one_combined_call(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES)

    apply.apply_relabel(
        conn,
        svc,
        "run1",
        "m1",
        add_names=[FULL_NAME["Personal"]],
        remove_names=[FULL_NAME["Work"]],
        actor="human-chat",
    )

    assert len(svc.calls) == 1  # one modify carries both add and remove
    _, body = svc.calls[0]
    assert body["addLabelIds"] == [f"id_{FULL_NAME['Personal']}"]
    assert body["removeLabelIds"] == [f"id_{FULL_NAME['Work']}"]

    # audit-before-write for both action types, actor threaded through
    rows = conn.execute(
        "SELECT action_type, status, actor FROM action_events ORDER BY event_id"
    ).fetchall()
    intended = [r for r in rows if r["status"] == "intended"]
    assert {r["action_type"] for r in intended} == {"label_add", "label_remove"}
    assert all(r["actor"] == "human-chat" for r in rows)
    current = conn.execute("SELECT status FROM current_actions").fetchall()
    assert len(current) == 2 and all(r["status"] == "confirmed" for r in current)


def test_relabel_empty_is_a_noop(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES)

    apply.apply_relabel(
        conn, svc, "run1", "m1", add_names=[], remove_names=[], actor="human-chat"
    )

    assert svc.calls == []
    assert conn.execute("SELECT COUNT(*) FROM action_events").fetchone()[0] == 0


def test_relabel_gmail_failure_records_failed_and_reraises(tmp_path):
    conn = _fresh_conn(tmp_path)
    svc = FakeService(ALL_LABEL_NAMES, fail=True)

    with pytest.raises(RuntimeError, match="gmail outage"):
        apply.apply_relabel(
            conn,
            svc,
            "run1",
            "m1",
            add_names=[FULL_NAME["Personal"]],
            remove_names=[FULL_NAME["Work"]],
            actor="human-chat",
        )

    rows = conn.execute("SELECT status FROM current_actions").fetchall()
    assert len(rows) == 2 and all(r["status"] == "failed" for r in rows)
