"""Corrections: Flow B `correct()` and Flow A `detect_relabels()`.

Hand-rolled fake Gmail service (labels().list, messages().get(minimal),
messages().modify) against a real tmp SQLite — mirrors test_apply.py's style.
"""

import pytest

from assistant import correct, store
from assistant.labels import FULL_NAME

ALL_LABEL_NAMES = list(FULL_NAME.values())


class _Exec:
    def __init__(self, result, on_execute=None):
        self._result, self._on_execute = result, on_execute

    def execute(self):
        if self._on_execute:
            self._on_execute()
        return self._result


class FakeService:
    """svc.users().labels().list / .messages().get(minimal) / .messages().modify.

    `msg_labels` maps message id -> its current Gmail label ids (mutated in place by
    modify, so a follow-up get sees the applied change). `fail_modify` makes modify
    raise, to exercise the Gmail-first failure path."""

    def __init__(self, msg_labels, fail_modify=False):
        self.calls: list[tuple[str, dict]] = []
        self.get_count = 0
        self._labels = [{"id": f"id_{n}", "name": n} for n in ALL_LABEL_NAMES]
        self._msg_labels = msg_labels
        self._fail = fail_modify

    def users(self):
        return self

    def labels(self):
        return self

    def list(self, userId):
        return _Exec({"labels": self._labels})

    def messages(self):
        return self

    def get(self, userId, id, format):
        self.get_count += 1
        return _Exec({"id": id, "labelIds": list(self._msg_labels.get(id, []))})

    def modify(self, userId, id, body):
        def _apply():
            labels = set(self._msg_labels.get(id, []))
            labels |= set(body.get("addLabelIds", []))
            labels -= set(body.get("removeLabelIds", []))
            self._msg_labels[id] = sorted(labels)
            self.calls.append((id, body))

        if self._fail:
            return _Exec(None, on_execute=_raise)
        return _Exec({}, on_execute=_apply)


def _raise():
    raise RuntimeError("gmail outage")


def _conn(tmp_path):
    conn = store.open_db(tmp_path / "triage.db")
    conn.execute(
        "INSERT INTO messages(gmail_message_id, first_seen_at) VALUES ('m1', ?)",
        (store.now_iso(),),
    )
    conn.commit()
    return conn


def _seed_verdict(conn, mid, category, priority=None, source="worker"):
    conn.execute(
        "INSERT INTO classifications"
        "(gmail_message_id, category, priority, classified_at, source) "
        "VALUES (?,?,?,?,?)",
        (mid, category, priority, store.now_iso(), source),
    )
    conn.commit()


def _latest(conn, mid):
    return conn.execute(
        "SELECT category, priority, source, reasoning, llm_call_id "
        "FROM current_classifications WHERE gmail_message_id=?",
        (mid,),
    ).fetchone()


# --- Flow B: correct() -------------------------------------------------------


def test_correct_relabels_gmail_and_records_human_chat(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    svc = FakeService({"m1": ["INBOX", f"id_{FULL_NAME['Work']}"]})

    summary = correct.correct(conn, svc, "m1", "Personal", None)

    _, body = svc.calls[0]
    assert body["addLabelIds"] == [f"id_{FULL_NAME['Personal']}"]
    assert body["removeLabelIds"] == [f"id_{FULL_NAME['Work']}"]
    row = _latest(conn, "m1")
    assert (row["category"], row["source"]) == ("Personal", "human-chat")
    assert row["llm_call_id"] is None and "was Work" in row["reasoning"]
    assert "Work -> Personal" in summary


def test_correct_omitting_priority_clears_the_priority_label(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work", "P1-Urgent")
    svc = FakeService(
        {"m1": ["INBOX", f"id_{FULL_NAME['Work']}", f"id_{FULL_NAME['P1-Urgent']}"]}
    )

    correct.correct(conn, svc, "m1", "Personal", None)

    _, body = svc.calls[0]
    assert set(body["removeLabelIds"]) == {
        f"id_{FULL_NAME['Work']}",
        f"id_{FULL_NAME['P1-Urgent']}",  # priority label cleared
    }


def test_correct_no_change_when_already_correct(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Personal")
    svc = FakeService({"m1": ["INBOX", f"id_{FULL_NAME['Personal']}"]})

    summary = correct.correct(conn, svc, "m1", "Personal", None)

    assert svc.calls == []  # no Gmail modify
    assert "no change" in summary
    # no new classification row appended
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1


def test_correct_unknown_message_raises(tmp_path):
    conn = _conn(tmp_path)
    svc = FakeService({})
    with pytest.raises(ValueError, match="unknown message id"):
        correct.correct(conn, svc, "nope", "Personal", None)


def test_correct_gmail_failure_writes_no_classification_row(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    svc = FakeService({"m1": ["INBOX", f"id_{FULL_NAME['Work']}"]}, fail_modify=True)

    with pytest.raises(RuntimeError, match="gmail outage"):
        correct.correct(conn, svc, "m1", "Personal", None)

    # Gmail-first: the classification row is only written after a clean relabel.
    row = _latest(conn, "m1")
    assert (row["category"], row["source"]) == ("Work", "worker")


# --- Flow A: detect_relabels() -----------------------------------------------


def _events(*mids):
    # a taxonomy label id changed on each message (the exact id is irrelevant; the
    # intersection with taxonomy ids is all detect_relabels checks to pick candidates)
    tax_id = f"id_{FULL_NAME['Personal']}"
    return [(mid, frozenset({tax_id})) for mid in mids]


def test_detect_records_one_category_drift(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    # the user relabeled Work -> Personal in Gmail; labels now show Personal.
    svc = FakeService({"m1": ["INBOX", f"id_{FULL_NAME['Personal']}"]})

    n = correct.detect_relabels(conn, svc, _events("m1"))

    assert n == 1
    row = _latest(conn, "m1")
    assert (row["category"], row["source"]) == ("Personal", "human-gmail")


def test_detect_removal_records_unclassified(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    svc = FakeService({"m1": ["INBOX"]})  # taxonomy label removed, none added

    n = correct.detect_relabels(conn, svc, _events("m1"))

    assert n == 1
    assert _latest(conn, "m1")["category"] == "UNCLASSIFIED"


def test_detect_skips_when_labels_match_verdict(tmp_path):
    # The worker's own apply (or a prior correct) leaves labels == verdict — not drift.
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    svc = FakeService({"m1": ["INBOX", f"id_{FULL_NAME['Work']}"]})

    assert correct.detect_relabels(conn, svc, _events("m1")) == 0
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1


def test_detect_ignores_stacked_categories(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    # both Work and Personal present — ambiguous, never guess.
    svc = FakeService(
        {"m1": ["INBOX", f"id_{FULL_NAME['Work']}", f"id_{FULL_NAME['Personal']}"]}
    )

    assert correct.detect_relabels(conn, svc, _events("m1")) == 0


def test_detect_is_idempotent_on_replay(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    svc = FakeService({"m1": ["INBOX", f"id_{FULL_NAME['Personal']}"]})

    assert correct.detect_relabels(conn, svc, _events("m1")) == 1
    # same history window replayed: verdict now Personal == labels, so no new row.
    assert correct.detect_relabels(conn, svc, _events("m1")) == 0
    human_rows = conn.execute(
        "SELECT COUNT(*) FROM classifications WHERE source='human-gmail'"
    ).fetchone()[0]
    assert human_rows == 1


def test_detect_skips_never_classified_message(tmp_path):
    conn = _conn(tmp_path)  # m1 exists but was never classified
    svc = FakeService({"m1": ["INBOX", f"id_{FULL_NAME['Personal']}"]})

    assert correct.detect_relabels(conn, svc, _events("m1")) == 0
    assert svc.get_count == 0  # skipped before any messages.get


def test_detect_no_events_is_noop(tmp_path):
    conn = _conn(tmp_path)
    _seed_verdict(conn, "m1", "Work")
    svc = FakeService({"m1": ["INBOX"]})
    assert correct.detect_relabels(conn, svc, []) == 0
    assert svc.calls == []
