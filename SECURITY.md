# Security Policy

This project reads a person's entire mailbox and holds an OAuth token that can
modify Gmail. Please treat security reports seriously.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository (Security → Report a vulnerability). Include steps to
reproduce and the impact you have in mind. You'll get an acknowledgement, and a
fix or mitigation plan once the report is triaged.

## What this software can and cannot do

These are structural properties of the code, useful when reasoning about risk.

- **It can never send or delete email.** The Gmail scope is `gmail.modify`
  (label/archive/draft only), and there is no `messages.send`, `messages.trash`,
  or `messages.delete` call anywhere in the source. "Never send, never delete" is
  a code-level guarantee, not just a prompt instruction.
- **The unattended worker's writes are gated by `dry_run`.** With
  `[triage] dry_run = true` (the shipped default) the worker classifies but makes
  no Gmail changes. Flipping it to `false` is the deliberate go-live step.
- **`dry_run` does NOT gate the chat agent's four bounded verbs**
  (`correct`, `create-draft`, `calendar create/move/delete`). Those are always
  live and always user-initiated through the Telegram agent. They are contained
  by other means: drafts are never sent, calendar move/delete refuse any event
  the assistant didn't create (an ownership marker is checked first), and the
  hermes gateway is locked to a single allowlisted chat id (fail-closed).

## Threat model notes

- **Prompt injection into the classifier.** Untrusted email content is fed to
  the model. Blast radius is tightly bounded: structured output constrained to a
  fixed enum, plus a database `CHECK` constraint, force every verdict onto the
  taxonomy. The worst a crafted email can do is steer the label/priority of
  *its own* message (e.g. force a P1 buzz, or — only if you enable
  `auto_archive_low_value` — get itself archived). The email content is wrapped
  in explicit untrusted-data framing as defense-in-depth.
- **Prompt injection into the improvement loop.** `assistant propose` feeds
  corrected-email text to a model whose output edits `rubric.md` / the skill
  file. The result is only ever a **draft pull request** a human must review and
  merge; a section splitter mechanically prevents edits to guardrails, taxonomy,
  code, or config. The human-merge gate is the mitigation.
- **Local data exposure.** The SQLite database stores full email bodies, and
  `secrets/` holds live credentials. Both are gitignored and never committed —
  but they are real files on disk. Publish this repo only via a clean
  `git clone`, never by zipping your working directory, and keep the `secrets/`
  and `data/` gitignore entries intact.

## Supported versions

This is a personal-use project without formal releases; fixes land on `main`.
Run the latest `main`.
