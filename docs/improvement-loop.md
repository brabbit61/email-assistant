# Agent-Improvement Loop

How corrections the user gives — in chat or by relabeling in Gmail — accumulate
and, on demand, become reviewable proposals to the rubric and skill file.
Nothing self-applies; every change lands as a diff the user approves.

## The loop

```
correction ──▶ captured (DB or memory) ──▶ accumulates
                                              │
        "propose improvements" (chat) ────────┤ on-demand review
                                              ▼
                             draft PR: rubric / skill edits + rationale
                                              ▼
                          user reviews → merge (= live) / edit / close
```

Capture is **continuous**; the review→proposal step is **on-demand only** (no
cron). You can't reconstruct a correction after the fact, so capture always runs;
proposing is when you ask.

## Capture — two channels

**1. Classification corrections → the DB** (a human-originated re-classification,
append-only, distinguishable from model verdicts by the `source` column). Two
flows converge here:

- **Flow A — you relabel in Gmail.** On its next poll the worker sees the
  message's current Gmail labels no longer match the taxonomy label it applied,
  and writes the human re-classification + a correction record. Gmail is already
  right; no agent involved.
- **Flow B — you correct in chat.** The agent resolves your words to a
  `gmail_message_id` and a taxonomy category/priority, then runs
  `assistant correct <id> --category X [--priority Y]`. That deterministic CLI
  verb reuses the worker's relabel logic to fix the Gmail label, then writes the
  same re-classification + correction record + audit rows. Synchronous — the
  agent confirms *"Done — moved to Personal."*

**2. Behavioral / style feedback → hermes memory.** Feedback that isn't a relabel
("digests too long", "put P1 first", "stop telling me to check Gmail myself") has
no message to reclassify. The agent writes a dated one-line note to its **own**
hermes memory — never `triage.db` on this path. Example:
`2026-07-14 — digests too verbose, wants P1 first`.

## What the review may propose

**Proposable — the loop's entire blast radius (behavior-steering markdown only):**
- `rubric.md` — category definitions, tie-breaks, P1 criteria. Live on merge
  (the classifier reads `rubric.md` fresh every call).
- The skill file's **digest-structure** section.
- The skill file's **conversation-playbook** section — refinements, examples.

**Immutable — never proposed:**
- The **hard guardrails** (read-only-except-`correct`/proposal-PR, never send,
  never delete, never click links).
- The **fixed taxonomy** (11 categories + 3 priorities — enum + CHECK + Gmail
  labels; mechanically immutable).
- The **architecture** and **any source code** (`.py`).
- **Config / tunables** (budget caps, model ids) — the owner edits
  `config.toml` directly.

One-sentence rule: *the loop reaches exactly the markdown that steers behavior —
never code, never config, never the taxonomy, never the guardrails.*

## Review & proposal

- **Trigger: on-demand from chat** — "propose improvements", "review my recent
  corrections". A new conversation intent in the playbook.
- **Window: watermark.** Reviews corrections newer than the last review run; you
  can widen it on request ("review everything", "since June"). Merged, edited, or
  rejected — reviewed corrections fall behind the watermark and never re-surface.
- **Judgment: pattern, not per-correction.** The review (Sonnet) proposes an edit
  only where it sees a recurring pattern; isolated one-offs are noted in the PR
  body without a change. No hardcoded threshold — the model judges. No pattern →
  **no PR** (silence is fine).
- **Surface: branch + draft PR** (`gh pr create --draft`), body explaining which
  corrections drove each edit. Review is a normal diff; **merge is the apply
  step** (rubric goes live immediately). Nothing self-merges.
- **Cost:** one Sonnet call per review, recorded in the cost ledger like any other.

## The loop's two bounded write powers

The conversation playbook made the agent read-only by default. This loop
grants two of its explicit, gated exceptions — nothing else changes:

1. **Apply a correction you gave it** — via `assistant correct` (Flow B).
2. **Open a proposal PR when you ask** — via `gh`, always a draft you review.

Both are initiated by you, both are visible (a Gmail relabel you can see, a PR you
approve), neither can self-apply a change to the system. Everything else — reading
the DB, summarizing, answering questions — stays strictly read-only.
