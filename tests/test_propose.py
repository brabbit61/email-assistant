"""On-demand review: `propose.propose` and its mechanical guardrail.

The git/gh mechanics are faked at the `subprocess.run` seam (records argv, returns
canned output, creates the worktree dirs so the real file writes land); the Sonnet
call is faked at `classify.make_client`. A real tmp SQLite holds the corrections.
"""

import json
import os
import subprocess

import pytest

from assistant import classify, propose, store
from assistant.config import Config, Secrets

SKILL_TEXT = (
    "# Skill\n\n"
    "## Digest structure\n\nold digest body\n\n"
    "## Conversation playbook\n\nold playbook body\n\n"
    "## Refusal behavior\n\nrefusal body\n"
)
RUBRIC_TEXT = "# Rubric\n\ncategory rules\n"
PR_URL = "https://github.com/owner/repo/pull/58"


class _FakeCompleted:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class FakeRun:
    """Records every subprocess call; returns canned output per command."""

    def __init__(self, *, gh_ok=True, gh_pr_fail=False, push_fail=False):
        self.calls: list[list[str]] = []
        self.gh_ok, self.gh_pr_fail, self.push_fail = gh_ok, gh_pr_fail, push_fail

    def __call__(self, cmd, cwd=None, check=False, capture_output=False, text=False):
        self.calls.append(list(cmd))
        if cmd[:3] == ["gh", "auth", "status"]:
            return _FakeCompleted(returncode=0 if self.gh_ok else 1)
        if cmd[:2] == ["git", "show"]:
            out = RUBRIC_TEXT if cmd[2].endswith("rubric.md") else SKILL_TEXT
            return _FakeCompleted(out)
        if cmd[:3] == ["git", "worktree", "add"]:
            tmp = cmd[4]  # git worktree add --detach <tmp> origin/main
            os.makedirs(os.path.join(tmp, "src", "assistant"), exist_ok=True)
            os.makedirs(os.path.join(tmp, "hermes", "email-assistant"), exist_ok=True)
            return _FakeCompleted("")
        if cmd[:2] == ["git", "push"] and self.push_fail:
            raise subprocess.CalledProcessError(1, cmd, stderr="push rejected")
        if cmd[:3] == ["gh", "pr", "create"]:
            if self.gh_pr_fail:
                raise subprocess.CalledProcessError(1, cmd, stderr="gh failed")
            return _FakeCompleted(PR_URL + "\n")
        return _FakeCompleted("")  # fetch, checkout, add, commit, worktree remove, ...

    def names(self):
        """First two tokens of each recorded command, for sequence assertions."""
        return [
            " ".join(c[:3]) if c[0] == "git" else " ".join(c[:3]) for c in self.calls
        ]


class _FakeLLM:
    class _Usage:
        input_tokens, output_tokens = 100, 50

    def __init__(self, payload=None, text=None):
        self._text = text if text is not None else json.dumps(payload)

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    def create(self, **kw):
        resp = type("R", (), {})()
        resp.usage = _FakeLLM._Usage()
        resp.content = [_FakeLLM._Block(self._text)]
        return resp


def _client(payload=None, text=None):
    return type("C", (), {"messages": _FakeLLM(payload, text)})()


def _cfg(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    return Config(
        root=tmp_path,
        client_secret_path=tmp_path / "cs.json",
        token_path=tmp_path / "tok.json",
        db_path=tmp_path / "data" / "triage.db",
        classifier_model="haiku",
        reviewer_model="claude-sonnet-5",
        monthly_usd_cap=15.0,
        daily_usd_soft_cap=0.75,
        dry_run=False,
        auto_archive_low_value=False,
        calendar_timezone="America/Los_Angeles",
        secrets=Secrets("sk-ant-test", "tok", "chat"),
    )


def _conn(tmp_path):
    return store.open_db(tmp_path / "data" / "triage.db")


def _seed_correction(conn, mid="m1", category="Personal", source="human-chat"):
    conn.execute(
        "INSERT INTO messages(gmail_message_id, sender, subject, first_seen_at) "
        "VALUES (?, 'a@b.com', ?, ?)",
        (mid, f"subj {mid}", store.now_iso()),
    )
    conn.execute(
        "INSERT INTO classifications"
        "(gmail_message_id, category, priority, reasoning, classified_at, source) "
        "VALUES (?, ?, NULL, 'corrected', ?, ?)",
        (mid, category, store.now_iso(), source),
    )
    conn.commit()
    return conn.execute("SELECT MAX(id) AS id FROM classifications").fetchone()["id"]


def _patch(monkeypatch, fake_run, client):
    monkeypatch.setattr(propose.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(propose.subprocess, "run", fake_run)
    monkeypatch.setattr(classify, "make_client", lambda key: client)


def _review_rows(conn):
    return conn.execute(
        "SELECT history_id, note FROM run_events WHERE phase='review' AND status='ok'"
    ).fetchall()


def _llm_rows(conn):
    return conn.execute(
        "SELECT actor, purpose, cost_usd FROM llm_calls WHERE purpose='propose'"
    ).fetchall()


# --- end-to-end propose() ----------------------------------------------------


def test_no_corrections_is_a_clean_noop(tmp_path, monkeypatch, capsys):
    conn = _conn(tmp_path)
    fake = FakeRun()
    _patch(
        monkeypatch, fake, _client({"edits": [], "cannot_propose": [], "one_offs": []})
    )

    code = propose.propose(conn, _cfg(tmp_path), notes=None, since=None)

    assert code == 0
    assert "No corrections to review." in capsys.readouterr().out
    assert fake.calls == [["gh", "auth", "status"]]  # preflight only, no LLM, no git
    assert _llm_rows(conn) == []
    assert _review_rows(conn) == []


def test_pattern_opens_draft_pr(tmp_path, monkeypatch, capsys):
    conn = _conn(tmp_path)
    max_id = _seed_correction(conn)
    fake = FakeRun()
    payload = {
        "edits": [
            {"target": "rubric", "new_text": "NEW RUBRIC", "rationale": "3 bank stmts"}
        ],
        "cannot_propose": [],
        "one_offs": [],
    }
    _patch(monkeypatch, fake, _client(payload))

    code = propose.propose(conn, _cfg(tmp_path), notes="digests too long", since=None)

    assert code == 0
    assert f"Opened draft PR: {PR_URL}" in capsys.readouterr().out
    # the full mechanical sequence ran
    joined = [" ".join(c) for c in fake.calls]
    for step in (
        "git fetch origin main",
        "git show origin/main:src/assistant/rubric.md",
        "git show origin/main:hermes/email-assistant/SKILL.md",
        "git worktree add",
        "git checkout -b",
        "git add -A",
        "git commit -m",
        "git push -u",
        "gh pr create",
        "git worktree remove",
    ):
        assert any(step in j for j in joined), f"missing {step!r} in {joined}"
    # watermark advanced to the max correction id, note = PR URL
    rows = _review_rows(conn)
    assert len(rows) == 1 and rows[0]["history_id"] == str(max_id)
    assert rows[0]["note"] == PR_URL
    # cost booked to the ledger under the reviewer actor
    llm = _llm_rows(conn)
    assert len(llm) == 1 and llm[0]["actor"] == "reviewer" and llm[0]["cost_usd"] > 0


def test_no_pattern_records_watermark_without_pr(tmp_path, monkeypatch, capsys):
    conn = _conn(tmp_path)
    max_id = _seed_correction(conn)
    fake = FakeRun()
    _patch(
        monkeypatch,
        fake,
        _client({"edits": [], "cannot_propose": [], "one_offs": ["one mislabel"]}),
    )

    code = propose.propose(conn, _cfg(tmp_path), notes=None, since=None)

    assert code == 0
    out = capsys.readouterr().out
    assert "No recurring pattern" in out
    assert "one-off (noted, no change): one mislabel" in out
    # no worktree/commit/push/gh after reading the base texts
    assert "git worktree add" not in fake.names()
    assert "gh pr create" not in fake.names()
    rows = _review_rows(conn)
    assert len(rows) == 1 and rows[0]["note"] == "no pattern"
    assert rows[0]["history_id"] == str(max_id)  # watermark still advances


def test_cannot_propose_is_surfaced(tmp_path, monkeypatch, capsys):
    conn = _conn(tmp_path)
    _seed_correction(conn)
    fake = FakeRun()
    _patch(
        monkeypatch,
        fake,
        _client(
            {
                "edits": [],
                "cannot_propose": [
                    "would require disabling the never-delete guardrail"
                ],
                "one_offs": [],
            }
        ),
    )

    propose.propose(conn, _cfg(tmp_path), notes=None, since=None)

    out = capsys.readouterr().out
    assert "cannot propose — guardrail: would require disabling" in out


def test_since_widens_past_the_watermark(tmp_path, monkeypatch, capsys):
    conn = _conn(tmp_path)
    cid = _seed_correction(conn)
    # advance the watermark past this correction: a normal run would see nothing.
    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, history_id, recorded_at) "
        "VALUES (?, 'review', 'ok', ?, ?)",
        (store.new_id(), str(cid), store.now_iso()),
    )
    conn.commit()
    fake = FakeRun()
    _patch(
        monkeypatch, fake, _client({"edits": [], "cannot_propose": [], "one_offs": []})
    )

    # without --since: nothing new past the watermark
    assert propose.propose(conn, _cfg(tmp_path), notes=None, since=None) == 0
    assert "No corrections to review." in capsys.readouterr().out

    # with --since: the window reopens and the review runs
    assert (
        propose.propose(conn, _cfg(tmp_path), notes=None, since="2020-01-01T00:00:00Z")
        == 0
    )
    assert "No recurring pattern" in capsys.readouterr().out


def test_gh_missing_fails_before_any_work(tmp_path, monkeypatch, capsys):
    conn = _conn(tmp_path)
    _seed_correction(conn)
    fake = FakeRun()
    monkeypatch.setattr(propose.shutil, "which", lambda _: None)
    monkeypatch.setattr(propose.subprocess, "run", fake)
    monkeypatch.setattr(classify, "make_client", lambda key: _client({}))

    code = propose.propose(conn, _cfg(tmp_path), notes=None, since=None)

    assert code == 1
    assert "gh not found" in capsys.readouterr().err
    assert fake.calls == []  # returned before touching git or the DB
    assert _review_rows(conn) == []


def test_push_failure_exits_one_leaves_no_watermark_and_cleans_up(
    tmp_path, monkeypatch, capsys
):
    conn = _conn(tmp_path)
    _seed_correction(conn)
    fake = FakeRun(push_fail=True)
    payload = {
        "edits": [{"target": "rubric", "new_text": "X", "rationale": "y"}],
        "cannot_propose": [],
        "one_offs": [],
    }
    _patch(monkeypatch, fake, _client(payload))

    code = propose.propose(conn, _cfg(tmp_path), notes=None, since=None)

    assert code == 1
    assert "PR creation failed" in capsys.readouterr().err
    assert _review_rows(conn) == []  # no watermark row -> re-runnable
    assert "git worktree remove" in fake.names()  # cleanup still ran


def test_malformed_response_books_cost_but_no_watermark(tmp_path, monkeypatch, capsys):
    conn = _conn(tmp_path)
    _seed_correction(conn)
    fake = FakeRun()
    _patch(monkeypatch, fake, _client(text="not json at all"))

    code = propose.propose(conn, _cfg(tmp_path), notes=None, since=None)

    assert code == 1
    assert "review failed" in capsys.readouterr().err
    assert len(_llm_rows(conn)) == 1  # cost recorded before the parse
    assert _review_rows(conn) == []  # watermark untouched


# --- mechanical guardrail (unit) ---------------------------------------------


def test_apply_edits_rubric_only():
    base = {"rubric": RUBRIC_TEXT, "skill": SKILL_TEXT}
    files = propose._apply_edits(
        [{"target": "rubric", "new_text": "NEW", "rationale": "r"}], base
    )
    assert files == {"src/assistant/rubric.md": "NEW"}  # skill untouched


def test_apply_edits_skill_section_isolated():
    base = {"rubric": RUBRIC_TEXT, "skill": SKILL_TEXT}
    files = propose._apply_edits(
        [
            {
                "target": "skill-playbook",
                "new_text": "brand new playbook",
                "rationale": "r",
            }
        ],
        base,
    )
    out = files["hermes/email-assistant/SKILL.md"]
    assert "brand new playbook" in out
    assert "## Conversation playbook" in out  # heading preserved
    assert "old digest body" in out  # digest section byte-for-byte intact
    assert "refusal body" in out  # everything after the section intact
    assert "old playbook body" not in out  # the body was replaced


def test_apply_edits_duplicate_target_raises():
    base = {"rubric": RUBRIC_TEXT, "skill": SKILL_TEXT}
    with pytest.raises(ValueError, match="duplicate target"):
        propose._apply_edits(
            [
                {"target": "rubric", "new_text": "a", "rationale": "r"},
                {"target": "rubric", "new_text": "b", "rationale": "r"},
            ],
            base,
        )


def test_replace_section_missing_heading_raises():
    with pytest.raises(ValueError, match="heading not found"):
        propose._replace_section("# no sections here\n", "## Nope", "x")


def test_pr_body_includes_rationale_oneoffs_and_cannot_propose():
    body = propose._pr_body(
        {
            "edits": [{"target": "rubric", "new_text": "x", "rationale": "why-here"}],
            "one_offs": ["a one-off"],
            "cannot_propose": ["a guardrail thing"],
        }
    )
    assert "why-here" in body
    assert "a one-off" in body
    assert "a guardrail thing" in body
