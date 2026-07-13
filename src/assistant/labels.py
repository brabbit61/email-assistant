"""Gmail label taxonomy: idempotent creation of the fixed taxonomy (T1.4, issue #10).

The taxonomy is fixed by design (PLAN.md) — the classifier (#11) may only
choose from `CATEGORIES`, never invent a label. This module owns the one
taxonomy definition plus the label spec (name, color, visibility) signed off
by Jenit, and the idempotent `reconcile()` that turns the spec into real
Gmail labels: `uv run python -m assistant.labels`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from googleapiclient.discovery import Resource

from assistant.config import load
from assistant.gmail import get_credentials, service

PARENT = "Assistant"

# (key, emoji, background, text) — colors must come from Gmail's fixed palette.
_CATEGORY_SPECS = [
    ("Action-Needed", "⚡", "#ff7537", "#ffffff"),
    ("Finance", "💰", "#d5ae49", "#000000"),
    ("Bills", "🧾", "#f2c960", "#000000"),
    ("Orders", "📦", "#fcda83", "#000000"),
    ("Events", "📅", "#149e60", "#ffffff"),
    ("Travel", "✈️", "#43d692", "#000000"),
    ("Work", "💼", "#8e63ce", "#ffffff"),
    ("Personal", "👤", "#b694e8", "#000000"),
    ("Dev", "💻", "#2da2bb", "#ffffff"),
    ("Newsletters", "📰", "#999999", "#ffffff"),
    ("Low-Value", "🗑", "#cccccc", "#000000"),
]
_PRIORITY_SPECS = [
    ("P1-Urgent", "🔴", "#fb4c2f", "#ffffff"),
    ("P2-This-Week", "🟡", "#ffad47", "#000000"),
    ("P3-FYI", "🔵", "#4a86e8", "#ffffff"),
]

CATEGORIES = [key for key, *_ in _CATEGORY_SPECS]
PRIORITIES = [key for key, *_ in _PRIORITY_SPECS]


@dataclass(frozen=True)
class LabelSpec:
    key: str
    bg: str
    text: str
    list_visibility: str  # "labelShow" | "labelShowIfUnread"
    leaf: str  # emoji + name, e.g. "⚡ Action-Needed"

    @property
    def full_name(self) -> str:
        return f"{PARENT}/{self.leaf}"


def _specs(
    rows: list[tuple[str, str, str, str]], list_visibility: str
) -> list[LabelSpec]:
    return [
        LabelSpec(
            key=key,
            bg=bg,
            text=text,
            list_visibility=list_visibility,
            leaf=f"{emoji} {key}",
        )
        for key, emoji, bg, text in rows
    ]


LABELS = _specs(_CATEGORY_SPECS, "labelShow") + _specs(
    _PRIORITY_SPECS, "labelShowIfUnread"
)

FULL_NAME = {
    spec.key: spec.full_name for spec in LABELS
}  # taxonomy key -> "Assistant/..."


@dataclass
class ReconcileResult:
    ids: dict[str, str]  # full_name -> Gmail label id
    created: list[str]
    updated: list[str]
    unchanged: list[str]


def _desired_body(spec: LabelSpec) -> dict:
    return {
        "name": spec.full_name,
        "messageListVisibility": "show",
        "labelListVisibility": spec.list_visibility,
        "color": {"backgroundColor": spec.bg, "textColor": spec.text},
    }


def _matches(existing: dict, spec: LabelSpec) -> bool:
    color = existing.get("color") or {}
    return (
        existing.get("messageListVisibility") == "show"
        and existing.get("labelListVisibility") == spec.list_visibility
        and color.get("backgroundColor") == spec.bg
        and color.get("textColor") == spec.text
    )


def label_ids(svc: Resource) -> dict[str, str]:
    """Live full_name -> id lookup, no create/patch. The applier (T1.7) calls this
    at the start of each run; labels are assumed already reconciled (T1.4)."""
    existing = svc.users().labels().list(userId="me").execute().get("labels", [])
    names = {spec.full_name for spec in LABELS}
    return {label["name"]: label["id"] for label in existing if label["name"] in names}


def reconcile(svc: Resource) -> ReconcileResult:
    """Idempotent upsert: create missing labels, patch drifted ones, skip the rest."""
    labels_api = svc.users().labels()
    existing = {
        label["name"]: label
        for label in labels_api.list(userId="me").execute().get("labels", [])
    }

    if PARENT not in existing:
        existing[PARENT] = labels_api.create(
            userId="me", body={"name": PARENT, "labelListVisibility": "labelShow"}
        ).execute()

    ids: dict[str, str] = {}
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    for spec in LABELS:
        current = existing.get(spec.full_name)
        if current is None:
            result = labels_api.create(userId="me", body=_desired_body(spec)).execute()
            created.append(spec.full_name)
        elif not _matches(current, spec):
            result = labels_api.patch(
                userId="me", id=current["id"], body=_desired_body(spec)
            ).execute()
            updated.append(spec.full_name)
        else:
            result = current
            unchanged.append(spec.full_name)
        ids[spec.full_name] = result["id"]

    return ReconcileResult(
        ids=ids, created=created, updated=updated, unchanged=unchanged
    )


def main() -> int:
    """`uv run python -m assistant.labels` — create/reconcile the taxonomy labels."""
    config = load()
    creds = get_credentials(
        config
    )  # non-interactive: raises AuthError loudly if unauthorized
    result = reconcile(service(creds))
    print(
        f"Labels reconciled: {len(result.created)} created, "
        f"{len(result.updated)} updated, {len(result.unchanged)} unchanged."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
