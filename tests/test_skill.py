"""Structural check on the hermes skill file: frontmatter parses
and the sections the improvement loop diffs against are present. Content
correctness (does the agent actually answer right) is the user's live check —
this test only guards against a broken file shipping.
"""

import re
from pathlib import Path

import yaml

SKILL_PATH = Path(__file__).parent.parent / "hermes" / "email-assistant" / "SKILL.md"


def _frontmatter_and_body(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


def test_skill_file_exists():
    assert SKILL_PATH.is_file()


def test_frontmatter_has_name_and_description():
    fm, _ = _frontmatter_and_body(SKILL_PATH.read_text())
    assert fm["name"] == "email-assistant"
    assert fm["description"]


def test_description_and_tags_beat_generic_email_skills_on_routing_keywords():
    """Regression guard: the router picked the generic `himalaya` IMAP skill
    over this one for "what's urgent in my email" because our description
    was jargon-first and we shipped no tags at all (himalaya ships explicit
    Email/IMAP/SMTP tags). Keep the words a router actually matches on."""
    fm, _ = _frontmatter_and_body(SKILL_PATH.read_text())
    description = fm["description"].lower()
    for word in ("email", "urgent", "inbox"):
        assert word in description
    tags = [t.lower() for t in fm["metadata"]["hermes"]["tags"]]
    assert "email" in tags
    assert "gmail" in tags


def test_loop_editable_sections_present():
    _, body = _frontmatter_and_body(SKILL_PATH.read_text())
    assert "## Hard guardrails" in body
    assert "## Digest structure" in body
    assert "## Conversation playbook" in body


def test_skill_references_the_open_verb_for_still_open_checks():
    _, body = _frontmatter_and_body(SKILL_PATH.read_text())
    assert "assistant open" in body


def test_skill_references_calendar_verbs_for_scheduling():
    _, body = _frontmatter_and_body(SKILL_PATH.read_text())
    assert "assistant calendar" in body


def test_bounded_write_count_is_consistent_everywhere():
    """The bounded-writes list is enumerated in more than one place; adding a
    verb to one site and missing another ships a contradiction (the edit
    briefly said both "three bounded" and "four bounded"). Every "<N> bounded"
    phrase in the file must agree."""
    _, body = _frontmatter_and_body(SKILL_PATH.read_text())
    counts = set(re.findall(r"\b(two|three|four|five|six)\s+bounded\b", body, re.I))
    assert len(counts) == 1, f"conflicting bounded-write counts: {sorted(counts)}"
