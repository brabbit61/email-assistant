"""The `assistant` CLI: `run [--dry-run]`, `status`, `audit [--since]`, `review
[--since]`, `costs [--month]`, `open`, `import-hermes [--hermes-db]` (T1.8 issue
#14; `review` added in T1.11 issue #17; `open` added in T2.4 issue #44).

The seam between the deterministic worker and everything else: hermes runs these
subcommands verbatim (no MCP server, no RPC), cron/systemd read the exit code,
Jenit reads the stdout. `run` composes poll -> classify -> apply into one pass;
`status`/`audit`/`costs` are read-only reports over the same SQLite log so
nobody has to open the DB by hand. Plain tabular text only — no machine-readable
flag yet (added if Phase 2 hermes work shows a real need, per the ticket).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assistant import (
    apply,
    classify,
    config,
    correct,
    gmail,
    poll,
    propose,
    store,
    telegram,
)
from assistant.classify import UNCLASSIFIED, Email, Verdict
from assistant.labels import CATEGORIES, PRIORITIES
from assistant.pricing import PRICES

# --- run ---------------------------------------------------------------------


def _messages_needing_classification(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """New messages, plus any whose latest classification is still UNCLASSIFIED —
    the retry behavior classify.py's own docstring promises. A human-recorded
    UNCLASSIFIED (a Gmail label removed by Jenit, source != 'worker') is *not*
    retried: the worker must not fight a correction."""
    return conn.execute(
        "SELECT m.gmail_message_id, m.sender, m.subject, m.body FROM messages m "
        "LEFT JOIN current_classifications c "
        "  ON c.gmail_message_id = m.gmail_message_id "
        "WHERE c.gmail_message_id IS NULL "
        "   OR (c.category = ? AND c.source = 'worker')",
        (UNCLASSIFIED,),
    ).fetchall()


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load()
    # config `dry_run` is the go-live gate the unattended timer obeys; --dry-run
    # force-dries a single manual invocation. Either one suppresses every write.
    dry = args.dry_run or cfg.dry_run
    conn = store.open_db(cfg.db_path)
    run_id = store.new_id()
    tok, chat = cfg.secrets.telegram_token, cfg.secrets.telegram_chat_id

    try:
        creds = gmail.get_credentials(cfg)
    except gmail.AuthError as e:
        # Auth dies before any run_event is written (so it never feeds the
        # failure counter); the OAuth ping fires regardless of dry_run (#43).
        telegram.notify_oauth_death(conn, run_id, tok, chat, str(e))
        raise  # main() prints it and exits 1

    svc = gmail.service(creds)
    client = classify.make_client(cfg.secrets.anthropic_api_key)

    conn.execute(
        "INSERT INTO run_events(run_id, phase, status, recorded_at) "
        "VALUES (?, 'triage', 'started', ?)",
        (run_id, store.now_iso()),
    )
    conn.commit()

    lines: list[str] = []
    labeled = 0
    errors = 0
    p1_hits: list[tuple[str, sqlite3.Row, Verdict]] = []
    try:
        poll_result = poll.poll_once(conn, svc)
        rows = _messages_needing_classification(conn)
        for row in rows:
            verdict, usage = classify.classify(
                client,
                cfg.classifier_model,
                Email(row["sender"], row["subject"], row["body"]),
            )
            classify.record(
                conn, row["gmail_message_id"], verdict, usage, actor="worker"
            )
            if verdict.category == UNCLASSIFIED:
                continue
            try:
                result = apply.apply_verdict(
                    conn,
                    svc,
                    run_id,
                    row["gmail_message_id"],
                    verdict,
                    actor="worker",
                    auto_archive=cfg.auto_archive_low_value,
                    dry_run=dry,
                )
            except Exception as e:  # one poisoned message must not abort the batch
                errors += 1
                lines.append(f"  ERROR    {row['gmail_message_id']}  {e}")
                continue
            labeled += 1
            if verdict.priority == "P1-Urgent":
                p1_hits.append((row["gmail_message_id"], row, verdict))
            tag = " ".join(result.labels)
            subject = (row["subject"] or "(no subject)")[:50]
            lines.append(f"  {row['gmail_message_id']:<16}  {tag:<28}  {subject}")

        # Ping is best-effort and isolated inside notify_p1: it never raises,
        # never touches run_events, and a dry run sends nothing (nothing's real).
        if p1_hits and not dry:
            telegram.notify_p1(conn, run_id, tok, chat, p1_hits)
    except Exception as e:
        # A crashed run (poll/classify blew up): record the triage terminal as
        # 'failed' — the streak signal notify_failure counts — then alert if the
        # streak hit the threshold, and let the error propagate (exit non-zero,
        # next timer tick retries). Operational pings ignore dry_run (#43).
        conn.execute(
            "INSERT INTO run_events(run_id, phase, status, recorded_at) "
            "VALUES (?, 'triage', 'failed', ?)",
            (run_id, store.now_iso()),
        )
        conn.commit()
        telegram.notify_failure(conn, run_id, tok, chat, str(e))
        raise

    # Clean completion: recovery ping if an alert is outstanding, then the daily
    # budget-breach check. Both fire regardless of dry_run (#43).
    telegram.notify_recovery(conn, run_id, tok, chat)
    telegram.notify_budget(
        conn,
        run_id,
        tok,
        chat,
        soft_cap=cfg.daily_usd_soft_cap,
        monthly_cap=cfg.monthly_usd_cap,
    )

    status = "ok" if errors == 0 else "error"
    conn.execute(
        "INSERT INTO run_events"
        "(run_id, phase, status, messages_seen, actions_taken, error_count, "
        " recorded_at) VALUES (?, 'triage', ?, ?, ?, ?, ?)",
        (
            run_id,
            status,
            poll_result.messages_seen,
            labeled,
            errors,
            store.now_iso(),
        ),
    )
    conn.commit()

    print(
        f"Poll: {poll_result.messages_seen} new messages "
        f"(historyId {poll_result.history_id})"
    )
    still_unclassified = len(rows) - labeled - errors  # verdict came back UNCLASSIFIED
    print(f"Classified: {len(rows)} ({still_unclassified} unclassified)")
    if lines:
        verb = "Would label" if dry else "Labeled"
        print(f"{verb} {labeled} message(s):")
        print("\n".join(lines))
    if dry:
        gate = (
            " (config gate — flip [triage] dry_run to go live)" if cfg.dry_run else ""
        )
        suffix = f" [DRY RUN — nothing written to Gmail]{gate}"
    else:
        suffix = ""
    print(f"{errors} error(s).{suffix}")
    return 1 if errors else 0


# --- status --------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = store.open_db(cfg.db_path)

    ckpt = conn.execute(
        "SELECT history_id, recorded_at FROM current_checkpoint"
    ).fetchone()
    last_run = conn.execute(
        "SELECT status, messages_seen, actions_taken, error_count, recorded_at "
        "FROM run_events WHERE phase='triage' AND status IS NOT NULL "
        "ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    unclassified = conn.execute(
        "SELECT COUNT(*) FROM current_classifications "
        "WHERE category = ? AND source = 'worker'",
        (UNCLASSIFIED,),
    ).fetchone()[0]
    unconfirmed = conn.execute(
        "SELECT COUNT(*) FROM current_actions WHERE status = 'intended'"
    ).fetchone()[0]
    db_size = cfg.db_path.stat().st_size / 1024 if cfg.db_path.exists() else 0.0

    if ckpt:
        print(
            f"Checkpoint:   historyId {ckpt['history_id']}, {store.age(ckpt['recorded_at'])}"
        )
    else:
        print("Checkpoint:   none — worker has never run")
    if last_run:
        print(
            f"Last run:     {last_run['status']} — {last_run['messages_seen']} "
            f"messages, {last_run['actions_taken']} actions, "
            f"{last_run['error_count']} errors ({store.age(last_run['recorded_at'])})"
        )
    else:
        print("Last run:     never")
    print(f"Unclassified: {unclassified} message(s) awaiting retry")
    print(
        f"Unconfirmed:  {unconfirmed} action(s)"
        + (" — crash recovery needed" if unconfirmed else "")
    )
    print(f"Database:     {cfg.db_path}, {db_size:.1f} KB")

    healthy = (
        ckpt is not None
        and (last_run is None or last_run["status"] == "ok")
        and unconfirmed == 0
    )
    return 0 if healthy else 1


# --- audit -----------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = store.open_db(cfg.db_path)
    since = args.since or (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # One line per logical action's *current* state (not every intended+confirmed
    # event row) — a confirmed action isn't "pending" just because it started life
    # as an intended row.
    rows = conn.execute(
        "SELECT ca.recorded_at, ca.status, ca.action_type, ca.gmail_message_id, "
        "       ca.detail, ca.error, cc.reasoning "
        "FROM current_actions ca "
        "LEFT JOIN current_classifications cc "
        "  ON cc.gmail_message_id = ca.gmail_message_id "
        "WHERE ca.recorded_at >= ? ORDER BY ca.recorded_at",
        (since,),
    ).fetchall()

    counts = {"confirmed": 0, "failed": 0, "intended": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        note = r["error"] or r["reasoning"] or ""
        print(
            f"{r['recorded_at']}  {r['status']:<9} {r['action_type']:<10} "
            f"{r['gmail_message_id']:<16}  {r['detail']:<28}  {note}"
        )
    print(
        f"{len(rows)} action(s) since {since} "
        f"({counts['confirmed']} confirmed, {counts['failed']} failed, "
        f"{counts['intended']} pending)"
    )
    return 0


# --- review ------------------------------------------------------------------


def cmd_review(args: argparse.Namespace) -> int:
    """Classifier verdicts for eyeball spot-checking during the dry-run trial —
    where `audit` is empty because dry runs write no action_events (T1.11)."""
    cfg = config.load()
    conn = store.open_db(cfg.db_path)
    since = args.since or (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    rows = conn.execute(
        "SELECT cc.classified_at, cc.category, cc.priority, cc.reasoning, "
        "       m.sender, m.subject "
        "FROM current_classifications cc "
        "JOIN messages m ON m.gmail_message_id = cc.gmail_message_id "
        "WHERE cc.classified_at >= ? ORDER BY cc.classified_at",
        (since,),
    ).fetchall()

    for r in rows:
        label = r["category"] + (f"/{r['priority']}" if r["priority"] else "")
        sender = (r["sender"] or "")[:24]
        subject = (r["subject"] or "(no subject)")[:40]
        print(
            f"{r['classified_at']}  {label:<24} {sender:<24}  {subject:<40}  "
            f"{r['reasoning'] or ''}"
        )
    print(f"{len(rows)} classification(s) since {since}")
    return 0


# --- costs -------------------------------------------------------------------


def cmd_costs(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = store.open_db(cfg.db_path)
    month = args.month or datetime.now(timezone.utc).strftime("%Y-%m")
    like = f"{month}%"

    total = (
        conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE created_at LIKE ?",
            (like,),
        ).fetchone()[0]
        or 0.0
    )
    pct = (total / cfg.monthly_usd_cap * 100) if cfg.monthly_usd_cap else 0.0
    print(
        f"{month} — ${total:.4f} of ${cfg.monthly_usd_cap:.4f} monthly cap ({pct:.0f}%)"
    )

    by_purpose = conn.execute(
        "SELECT purpose, actor, model, COUNT(*) AS calls, SUM(cost_usd) AS cost "
        "FROM llm_calls WHERE created_at LIKE ? "
        "GROUP BY purpose, actor, model ORDER BY cost DESC",
        (like,),
    ).fetchall()
    for r in by_purpose:
        print(
            f"  {r['purpose']:<10} {r['actor']:<8} {r['calls']:>4} calls   "
            f"${r['cost']:.4f}   {r['model']}"
        )

    days_seen = conn.execute(
        "SELECT COUNT(DISTINCT substr(created_at, 1, 10)) FROM llm_calls "
        "WHERE created_at LIKE ?",
        (like,),
    ).fetchone()[0]
    if days_seen:
        print(
            f"Daily average: ${total / days_seen:.4f}/day "
            f"(soft cap ${cfg.daily_usd_soft_cap:.4f}/day)"
        )

    daily = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, SUM(cost_usd) AS cost "
        "FROM llm_calls WHERE created_at LIKE ? GROUP BY day ORDER BY day",
        (like,),
    ).fetchall()
    if daily:
        print("By day:")
        for r in daily:
            print(f"  {r['day']}   ${r['cost']:.4f}")
    return 0


# --- import-hermes -------------------------------------------------------------
#
# hermes-agent is a separate process with its own Anthropic key and its own
# session log (~/.hermes/state.db) — nothing here ever sees those calls, so the
# cost ledger (`llm_calls`) undercounts vs. the Anthropic console by exactly
# hermes's spend. hermes records tokens per session but not USD (no local price
# for a model this new), so we price them ourselves and import one row per
# *finished* session (ended_at IS NOT NULL — a session's tokens are only final
# once it ends, so a still-open chat is picked up on a later run instead of
# double-counted). Rates come from the shared pricing.PRICES table.

_HERMES_DB_PATH = Path.home() / ".hermes" / "state.db"


def _hermes_session_cost(row: sqlite3.Row) -> float | None:
    """USD for one hermes session, or None if its model has no local price."""
    rates = PRICES.get(row["model"])
    if rates is None:
        return None
    return (
        row["input_tokens"] * rates["input"]
        + row["output_tokens"] * rates["output"]
        + row["cache_read_tokens"] * rates["cache_read"]
        + row["cache_write_tokens"] * rates["cache_write"]
    ) / 1_000_000


def cmd_import_hermes(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = store.open_db(cfg.db_path)

    hermes_db_path = Path(args.hermes_db)
    if not hermes_db_path.exists():
        print(f"No hermes session log at {hermes_db_path}")
        return 0

    hermes = sqlite3.connect(f"file:{hermes_db_path}?mode=ro", uri=True)
    hermes.row_factory = sqlite3.Row
    sessions = hermes.execute(
        "SELECT id, source, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, ended_at FROM sessions "
        "WHERE ended_at IS NOT NULL AND billing_provider = 'anthropic'"
    ).fetchall()
    hermes.close()

    imported, imported_cost, skipped_models = 0, 0.0, set()
    for s in sessions:
        cost = _hermes_session_cost(s)
        if cost is None:
            skipped_models.add(s["model"])
            continue
        created_at = datetime.fromtimestamp(s["ended_at"], tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO llm_calls(actor, purpose, model, input_tokens, "
            "output_tokens, cost_usd, created_at, hermes_session_id) "
            "VALUES ('hermes', ?, ?, ?, ?, ?, ?, ?)",
            (
                s["source"] or "chat",
                s["model"],
                s["input_tokens"],
                s["output_tokens"],
                cost,
                created_at,
                s["id"],
            ),
        )
        if cur.rowcount:
            imported += 1
            imported_cost += cost
    conn.commit()

    print(f"Imported {imported} hermes session(s), ${imported_cost:.4f}")
    if skipped_models:
        print(f"Skipped (no local price): {', '.join(sorted(skipped_models))}")
    return 0


# --- open --------------------------------------------------------------------


def _actionable_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The standing actionable set the digests/hermes rundown care about:
    Action-Needed by category, or P1/P2 by priority regardless of category."""
    return conn.execute(
        "SELECT cc.gmail_message_id, cc.category, cc.priority, cc.reasoning, "
        "       m.sender, m.subject, m.thread_id "
        "FROM current_classifications cc "
        "JOIN messages m ON m.gmail_message_id = cc.gmail_message_id "
        "WHERE cc.category = 'Action-Needed' "
        "   OR cc.priority IN ('P1-Urgent', 'P2-This-Week') "
        "ORDER BY cc.classified_at"
    ).fetchall()


def _thread_state(svc, thread_id: str, message_id: str) -> tuple[bool, bool, bool]:
    """One Gmail call → (read, archived, replied) for one actionable item.

    format='minimal' returns id + labelIds per message — enough for all three
    signals, no bodies fetched. 'replied' checks the whole thread for a SENT
    message (Jenit's own reply); 'read'/'archived' key off the classified
    message's own labels.
    """
    thread = (
        svc.users().threads().get(userId="me", id=thread_id, format="minimal").execute()
    )
    messages = thread.get("messages", [])
    own = next((m for m in messages if m["id"] == message_id), {})
    labels = set(own.get("labelIds", []))
    replied = any("SENT" in m.get("labelIds", []) for m in messages)
    return "UNREAD" not in labels, "INBOX" not in labels, replied


def cmd_open(args: argparse.Namespace) -> int:
    """Actionable set (Action-Needed / P1 / P2), cross-checked against live
    Gmail: cleared the moment Gmail shows it read, archived, or replied to —
    the disjunction of #37's three signals (open needs all three to fail).

    Read-only — the only Gmail call is threads().get. Never prints a partial
    list: a Gmail failure aborts loudly before anything is printed, since a
    half-verified "still open" list is worse than none (grounding rule, #37).
    """
    cfg = config.load()
    conn = store.open_db(cfg.db_path)
    rows = _actionable_rows(conn)
    if not rows:
        print("0 actionable in DB · 0 open · 0 cleared")
        return 0

    try:
        creds = gmail.get_credentials(cfg)
        svc = gmail.service(creds)
        states = [
            _thread_state(svc, r["thread_id"], r["gmail_message_id"]) for r in rows
        ]
    except Exception as e:
        print(f"Gmail check failed — no list printed: {e}", file=sys.stderr)
        return 1

    open_lines, cleared_lines = [], []
    for row, (read, archived, replied) in zip(rows, states):
        label = row["priority"] or row["category"]
        sender = (row["sender"] or "")[:24]
        reason = (row["reasoning"] or row["subject"] or "")[:60]
        line = f"  {label} · {sender} · {reason}"
        if replied or archived or read:
            why = "replied" if replied else "archived" if archived else "read"
            cleared_lines.append(f"{line} — {why}")
        else:
            open_lines.append(line)

    print(f"Open — Gmail-verified ({len(open_lines)}):")
    if open_lines:
        print("\n".join(open_lines))
    if cleared_lines:
        print(f"Cleared since triage ({len(cleared_lines)}):")
        print("\n".join(cleared_lines))
    print(
        f"{len(rows)} actionable in DB · {len(open_lines)} open · "
        f"{len(cleared_lines)} cleared"
    )
    return 0


# --- correct (improvement loop, Flow B) --------------------------------------


def cmd_correct(args: argparse.Namespace) -> int:
    """Apply a chat correction (intent 8): fix the Gmail label, record a
    human-originated re-classification. Synchronous, gated by Jenit's request —
    one of the agent's two bounded write powers (#40, #46)."""
    cfg = config.load()
    conn = store.open_db(cfg.db_path)
    try:
        creds = gmail.get_credentials(cfg)
        svc = gmail.service(creds)
        summary = correct.correct(conn, svc, args.id, args.category, args.priority)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    except gmail.AuthError:
        raise  # let main() format auth failures consistently
    except Exception as e:
        print(f"Correction failed — Gmail not modified cleanly: {e}", file=sys.stderr)
        return 1
    print(summary)
    return 0


# --- propose (improvement loop, on-demand review) ----------------------------


def cmd_propose(args: argparse.Namespace) -> int:
    """Review corrections since the last run (intent 9): one Sonnet call judges
    whether a recurring pattern warrants an edit to rubric.md / the skill's
    digest+playbook sections, and if so opens a draft PR. No pattern -> no PR.
    Nothing self-applies; Jenit reviews and merges."""
    cfg = config.load()
    conn = store.open_db(cfg.db_path)
    return propose.propose(conn, cfg, notes=args.notes, since=args.since)


# --- entry point ---------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assistant")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="one poll -> classify -> apply pass")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="checkpoint age, last run, health")
    p_status.set_defaults(func=cmd_status)

    p_audit = sub.add_parser("audit", help="chronological actions with reasoning")
    p_audit.add_argument("--since", default=None, help="ISO-8601 timestamp/date")
    p_audit.set_defaults(func=cmd_audit)

    p_review = sub.add_parser("review", help="classifier verdicts for spot-checking")
    p_review.add_argument("--since", default=None, help="ISO-8601 timestamp/date")
    p_review.set_defaults(func=cmd_review)

    p_costs = sub.add_parser("costs", help="spend by model/component vs budget cap")
    p_costs.add_argument("--month", default=None, help="YYYY-MM, default current month")
    p_costs.set_defaults(func=cmd_costs)

    p_open = sub.add_parser(
        "open", help="actionable set, Gmail-verified still-open (read-only)"
    )
    p_open.set_defaults(func=cmd_open)

    p_import_hermes = sub.add_parser(
        "import-hermes", help="pull finished hermes sessions into the cost ledger"
    )
    p_import_hermes.add_argument(
        "--hermes-db", default=str(_HERMES_DB_PATH), help="path to hermes's state.db"
    )
    p_import_hermes.set_defaults(func=cmd_import_hermes)

    p_correct = sub.add_parser(
        "correct", help="apply a human correction: fix the Gmail label, record it"
    )
    p_correct.add_argument("id", help="gmail_message_id to correct")
    p_correct.add_argument("--category", required=True, choices=CATEGORIES)
    p_correct.add_argument(
        "--priority",
        choices=PRIORITIES,
        default=None,
        help="omit to clear any priority label",
    )
    p_correct.set_defaults(func=cmd_correct)

    p_propose = sub.add_parser(
        "propose",
        help="review corrections since the last run; open a draft PR or report no pattern",
    )
    p_propose.add_argument(
        "--since", default=None, help="ISO-8601: widen the window past the watermark"
    )
    p_propose.add_argument(
        "--notes",
        default=None,
        help="behavioral/style notes (hermes memory) to fold in",
    )
    p_propose.set_defaults(func=cmd_propose)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    try:
        sys.exit(args.func(args))
    except (config.ConfigError, gmail.AuthError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
