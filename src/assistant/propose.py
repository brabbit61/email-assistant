"""On-demand improvement-loop review (spec docs/improvement-loop.md).

`assistant propose` reviews the classification corrections captured since the last
review (Flow A/B, `source != 'worker'`) plus the behavioral/style notes hermes
passes via `--notes`. One Sonnet call judges whether a *recurring pattern* warrants
an edit; if so it opens a **draft** PR editing only `rubric.md` and/or the skill's
Digest-structure / Conversation-playbook sections. No pattern -> no PR. Nothing
self-merges — merge is the apply step, always the user's.

The three editable targets are the loop's entire blast radius: the schema enum plus
the section splitter make the guardrails, taxonomy, code, and config *mechanically*
unreachable. A correction implying one of those comes back in `cannot_propose`,
never as an edit.

Watermark: a `run_events` row with `phase='review'` whose `history_id` holds the
max classification id considered — the same id-based checkpoint idiom the poller
uses. A failed run writes no row, so the next attempt re-reads the same corrections.

The PR is built in a throwaway `git worktree` off `origin/main`: the user's working
checkout is never the diff base and never touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from assistant import classify, config, store

_RUBRIC_PATH = "src/assistant/rubric.md"
_SKILL_PATH = "hermes/email-assistant/SKILL.md"
# target key -> the exact '## ' heading whose body it may replace.
_SKILL_SECTIONS = {
    "skill-digest": "## Digest structure",
    "skill-playbook": "## Conversation playbook",
}
_TARGETS = ["rubric", *_SKILL_SECTIONS]

_PR_TITLE = "Improvement loop: proposed rubric/skill edits"
_COMMIT_MSG = (
    "Improvement loop: proposed rubric/skill edits\n\n"
    "Proposed by `assistant propose`. Review and merge to apply."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": _TARGETS},
                    "new_text": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["target", "new_text", "rationale"],
                "additionalProperties": False,
            },
        },
        "cannot_propose": {"type": "array", "items": {"type": "string"}},
        "one_offs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["edits", "cannot_propose", "one_offs"],
    "additionalProperties": False,
}

_SYSTEM = """You review corrections the user made to an email-triage assistant \
and propose edits to its behaviour-steering markdown only when you see a recurring, \
generalizable pattern.

You may edit exactly three targets:
- rubric: the classifier's rubric (category definitions, tie-breaks, P1 criteria).
- skill-digest: the digest-structure section of the agent skill.
- skill-playbook: the conversation-playbook section of the agent skill.

For each edit return the COMPLETE new text of that target — a full replacement, not \
a diff — based on the current text given below, making the smallest change that \
addresses the pattern. For skill-digest and skill-playbook, return the section body \
WITHOUT the '## ...' heading line (the heading is kept for you).

Judgment: propose an edit only where several corrections point the same way. A single \
mislabel fix is not a pattern — list it in one_offs with no edit. No fixed threshold; \
use judgement. If nothing recurs, return no edits.

Immutable — never edit these; if a correction implies one, put a short explanation in \
cannot_propose instead:
- the hard guardrails (draft-never-send, never delete, never click links, read-only DB),
- the fixed taxonomy (the 11 categories and 3 priorities),
- the architecture or any source code,
- config / tunables (budget caps, model ids)."""


def propose(
    conn,
    cfg: config.Config,
    *,
    notes: str | None,
    since: str | None,
) -> int:
    """Run one review. Returns a CLI exit code (0 = handled, 1 = failed loudly)."""
    gh_err = _preflight_gh(cfg.root)
    if gh_err:
        print(gh_err, file=sys.stderr)
        return 1

    corrections = _corrections(conn, _watermark(conn), since)
    if not corrections:
        print("No corrections to review.")
        return 0
    max_id = max(c["id"] for c in corrections)

    try:
        _run(["git", "fetch", "origin", "main"], cfg.root)
        base = {
            "rubric": _run(["git", "show", f"origin/main:{_RUBRIC_PATH}"], cfg.root),
            "skill": _run(["git", "show", f"origin/main:{_SKILL_PATH}"], cfg.root),
        }
    except subprocess.CalledProcessError as e:
        print(f"git base read failed: {e.stderr or e}", file=sys.stderr)
        return 1

    client = classify.make_client(cfg.secrets.anthropic_api_key)
    try:
        result = _review(conn, client, cfg.reviewer_model, corrections, notes, base)
    except Exception as e:  # noqa: BLE001 — malformed/APIs: report, leave watermark
        print(f"review failed: {e}", file=sys.stderr)
        return 1

    if not result["edits"]:
        _record_watermark(conn, max_id, "no pattern")
        _report(result, url=None)
        return 0

    try:
        files = _apply_edits(result["edits"], base)
    except ValueError as e:
        print(f"malformed proposal: {e}", file=sys.stderr)
        return 1  # cost already booked; watermark untouched -> re-runnable

    try:
        url = _open_pr(cfg.root, files, result)
    except subprocess.CalledProcessError as e:
        print(f"PR creation failed: {e.stderr or e}", file=sys.stderr)
        return 1

    _record_watermark(conn, max_id, url)
    _report(result, url=url)
    return 0


# --- corrections + watermark -------------------------------------------------


def _watermark(conn) -> int:
    row = conn.execute(
        "SELECT history_id FROM run_events "
        "WHERE phase='review' AND status='ok' ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    return int(row["history_id"]) if row and row["history_id"] else 0


def _corrections(conn, watermark: int, since: str | None):
    """Human corrections to review, oldest first. `--since` widens the window to a
    date, ignoring the watermark; otherwise everything with a larger id than the
    last reviewed correction. Each row also carries the immediately-prior verdict."""
    prev = (
        "(SELECT p.{col} FROM classifications p "
        " WHERE p.gmail_message_id = c.gmail_message_id AND p.id < c.id "
        " ORDER BY p.id DESC LIMIT 1)"
    )
    select = (
        "SELECT c.id, c.gmail_message_id, c.category, c.priority, c.reasoning, "
        "       c.classified_at, c.source, m.sender, m.subject, "
        f"       {prev.format(col='category')} AS prev_category, "
        f"       {prev.format(col='priority')} AS prev_priority "
        "FROM classifications c JOIN messages m "
        "  ON m.gmail_message_id = c.gmail_message_id "
    )
    if since:
        return conn.execute(
            select + "WHERE c.source != 'worker' AND c.classified_at >= ? "
            "ORDER BY c.id",
            (since,),
        ).fetchall()
    return conn.execute(
        select + "WHERE c.source != 'worker' AND c.id > ? ORDER BY c.id",
        (watermark,),
    ).fetchall()


def _record_watermark(conn, max_id: int, note: str) -> None:
    conn.execute(
        "INSERT INTO run_events"
        "(run_id, phase, status, history_id, note, recorded_at) "
        "VALUES (?, 'review', 'ok', ?, ?, ?)",
        (store.new_id(), str(max_id), note, store.now_iso()),
    )
    conn.commit()


# --- the review call ---------------------------------------------------------


def _review(conn, client, model: str, corrections, notes: str | None, base: dict):
    """One Sonnet call. Records the cost ledger row BEFORE parsing, so a malformed
    response still books its cost; the parse error then propagates (watermark
    untouched, so the run is re-tried on the same corrections)."""
    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _user_prompt(corrections, notes, base)}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    usage = resp.usage
    conn.execute(
        "INSERT INTO llm_calls"
        "(actor, purpose, model, input_tokens, output_tokens, cost_usd, created_at) "
        "VALUES ('reviewer', 'propose', ?, ?, ?, ?, ?)",
        (
            model,
            usage.input_tokens,
            usage.output_tokens,
            classify.cost_usd(model, usage.input_tokens, usage.output_tokens),
            store.now_iso(),
        ),
    )
    conn.commit()
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _user_prompt(corrections, notes: str | None, base: dict) -> str:
    lines = ["# Corrections since the last review", ""]
    for c in corrections:
        prev = c["prev_category"] or "unclassified"
        if c["prev_priority"]:
            prev += f"/{c['prev_priority']}"
        now = c["category"] + (f"/{c['priority']}" if c["priority"] else "")
        note = f"  ({c['reasoning']})" if c["reasoning"] else ""
        lines.append(
            f"- [{c['classified_at']}, {c['source']}] from {c['sender']!r} "
            f"subj {c['subject']!r}: {prev} -> {now}{note}"
        )
    lines += [
        "",
        "# Behavioral / style notes (from the agent's memory)",
        notes.strip() if notes else "(none)",
        "",
        "# Current rubric.md",
        "```",
        base["rubric"],
        "```",
        "",
        "# Current skill sections (editable)",
        "```",
        _extract_section(base["skill"], _SKILL_SECTIONS["skill-digest"]),
        "",
        _extract_section(base["skill"], _SKILL_SECTIONS["skill-playbook"]),
        "```",
    ]
    return "\n".join(lines)


# --- mechanical guardrail: only two files, three regions ---------------------


def _apply_edits(edits, base: dict) -> dict[str, str]:
    """Return {relative_path: full new text} for the edited targets. Raises
    ValueError on a duplicate target or a skill section whose heading is missing —
    the enum already bounds `target` to the three allowed values."""
    seen: set[str] = set()
    rubric_text = base["rubric"]
    skill_text = base["skill"]
    for e in edits:
        target = e["target"]
        if target in seen:
            raise ValueError(f"duplicate target: {target}")
        seen.add(target)
        if target == "rubric":
            rubric_text = e["new_text"]
        else:
            skill_text = _replace_section(
                skill_text, _SKILL_SECTIONS[target], e["new_text"]
            )
    files: dict[str, str] = {}
    if "rubric" in seen:
        files[_RUBRIC_PATH] = rubric_text
    if seen & set(_SKILL_SECTIONS):
        files[_SKILL_PATH] = skill_text
    return files


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int]:
    """(heading index, index of the next '## ' or EOF). Raises if not found."""
    try:
        start = lines.index(heading)
    except ValueError:
        raise ValueError(f"section heading not found: {heading!r}") from None
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
        len(lines),
    )
    return start, end


def _replace_section(text: str, heading: str, new_body: str) -> str:
    """Replace the body under `heading` with `new_body`, keeping the heading line
    and everything before it / from the next '## ' heading onward."""
    lines = text.split("\n")
    start, end = _section_bounds(lines, heading)
    return "\n".join(lines[: start + 1] + new_body.split("\n") + lines[end:])


def _extract_section(text: str, heading: str) -> str:
    """The heading line plus its body, up to the next '## ' heading — for showing
    the model the current section text. Empty string if the heading is absent."""
    lines = text.split("\n")
    try:
        start, end = _section_bounds(lines, heading)
    except ValueError:
        return ""
    return "\n".join(lines[start:end])


# --- draft PR in a throwaway worktree ----------------------------------------


def _open_pr(root, files: dict[str, str], result: dict) -> str:
    """Commit the edits to a fresh branch off origin/main in a temp worktree, push,
    and open a draft PR against main. Returns the PR URL. Best-effort cleanup runs
    in `finally`; a pushed branch left behind after a `gh` failure is harmless litter
    (timestamped names never collide)."""
    branch = "improve/" + store.now_iso().replace(":", "").replace("-", "")
    tmp = tempfile.mkdtemp(prefix="propose-")
    try:
        _run(["git", "worktree", "add", "--detach", tmp, "origin/main"], root)
        _run(["git", "checkout", "-b", branch], tmp)
        for rel, text in files.items():
            (Path(tmp) / rel).write_text(text)
        _run(["git", "add", "-A"], tmp)
        _run(["git", "commit", "-m", _COMMIT_MSG], tmp)
        _run(["git", "push", "-u", "origin", branch], tmp)
        body_path = Path(tmp) / ".propose-pr-body.md"  # untracked; not committed
        body_path.write_text(_pr_body(result))
        out = _run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                _PR_TITLE,
                "--body-file",
                str(body_path),
            ],
            tmp,
        )
        return out.strip().splitlines()[-1]
    finally:
        _run_quiet(["git", "worktree", "remove", "--force", tmp], root)
        _run_quiet(["git", "branch", "-D", branch], root)


def _pr_body(result: dict) -> str:
    lines = ["Proposed by the improvement loop (`assistant propose`).", ""]
    lines.append("## Edits")
    lines += [f"- **{e['target']}** — {e['rationale']}" for e in result["edits"]]
    if result.get("one_offs"):
        lines += ["", "## One-offs (noted, not changed)"]
        lines += [f"- {o}" for o in result["one_offs"]]
    if result.get("cannot_propose"):
        lines += ["", "## Cannot propose — guardrail"]
        lines += [f"- {c}" for c in result["cannot_propose"]]
    lines += ["", "Draft — nothing self-merges; review and merge to apply."]
    return "\n".join(lines)


def _report(result: dict, url: str | None) -> None:
    print(f"Opened draft PR: {url}" if url else "No recurring pattern — no PR opened.")
    for c in result.get("cannot_propose", []):
        print(f"cannot propose — guardrail: {c}")
    for o in result.get("one_offs", []):
        print(f"one-off (noted, no change): {o}")


# --- subprocess helpers ------------------------------------------------------


def _preflight_gh(root) -> str | None:
    if shutil.which("gh") is None:
        return "gh not found on PATH — install it and run 'gh auth login'."
    r = subprocess.run(
        ["gh", "auth", "status"], cwd=root, capture_output=True, text=True
    )
    if r.returncode != 0:
        return "gh is not authenticated — run 'gh auth login'."
    return None


def _run(cmd: list[str], cwd) -> str:
    return subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _run_quiet(cmd: list[str], cwd) -> None:
    subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)  # best-effort cleanup
