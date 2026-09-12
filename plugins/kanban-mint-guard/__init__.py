"""Mint-time assignee routing guard, on upstream's ``pre_tool_call`` hook.

See plugin.yaml for why this is a plugin and not the reverted core patch.

Contract (hermes_cli/plugins.py):
    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

Imports NOTHING from hermes_cli. It decides on the tool arguments alone, so it has
no merge surface at all and cannot be broken by upstream moving a symbol.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

__all__ = ["register", "on_pre_tool_call", "verdict"]

logger = logging.getLogger(__name__)

# Profiles that are not board lanes. A build/review/verify/deploy card must never
# land on one of these. (axel: WeRoll business only; switch: switchboard; brain:
# takes no cards — it has no kanban toolset.)
NON_LANE = frozenset({"axel", "switch", "brain"})

# Lane -> the profile that owns it.
LANE_OWNER = {"build": "bob", "review": "rodge", "verify": "steve-o",
              "design": "karl", "deploy": "default", "pm": "jobsy"}

# Title verbs -> lane. Ordered: the first match wins, so the more specific
# review/verify verbs are tested before the broad build verbs.
LANE_PATTERNS = (
    ("review", re.compile(r"^\s*(\[rodge\]|rodge\s*[—–-]|re-?review\b|review\b)", re.I)),
    ("verify", re.compile(r"^\s*(\[steve-?o\]|steve-?o\s*[—–-]|qa\b|verify\b|re-?verify\b|real-click\b)", re.I)),
    ("deploy", re.compile(r"^\s*(\[deploy\]|deploy\b|ship\b|release\b|roll\s*out\b)", re.I)),
    ("design", re.compile(r"^\s*(\[karl\]|karl\s*[—–-]|spec\b|design\b)", re.I)),
    ("pm",     re.compile(r"^\s*(\[jobsy\]|jobsy\s*[—–-]|decompose\b|triage\b)", re.I)),
    ("build",  re.compile(r"^\s*(\[bob\]|bob\s*[—–-]|build\b|implement\b|fix\b|patch\b|repair\b|rework\b|plumb\b|add\b|create\b|restore\b|scaffold\b|migrate\b)", re.I)),
)

# An explicit owner marker anywhere at the start: "[Bob] ..." or "Bob — ...".
OWNER_MARKER = re.compile(r"^\s*(?:\[(?P<b>[a-z][a-z0-9._-]{1,20})\]|(?P<c>[a-z][a-z0-9._-]{1,20})\s*[—–]\s)", re.I)

KNOWN = frozenset({"bob", "rodge", "steve-o", "karl", "jobsy", "default", "axel", "switch", "brain"})
CROSS_LANE_BLOCK = frozenset({"rodge", "steve-o", "karl"})   # a build card on one of these
OVERRIDE = "assignee-override:"


TOPIC_TAG = re.compile(r"^\s*\[(?P<t>[a-z][a-z0-9 ._-]{1,20})\]\s*", re.I)


def _strip_topic_tag(title: str) -> str:
    """Drop a leading "[platform]"-style TOPIC tag so the lane verb after it is read.

    Only a tag that is NOT a profile name is stripped — "[Bob]" is an owner marker
    and must survive. Getting this wrong is what let
    "[platform] Plumb BACKUPBRAIN_API_KEY ..." -> axel through in the first draft;
    the negative control caught it.
    """
    m = TOPIC_TAG.match(title or "")
    if m and m.group("t").strip().lower() not in KNOWN:
        return (title or "")[m.end():]
    return title or ""


def _lane(title: str) -> Optional[str]:
    probe = _strip_topic_tag(title)
    for lane, pat in LANE_PATTERNS:
        if pat.match(title or "") or pat.match(probe):
            return lane
    return None


def _marker_owner(title: str) -> Optional[str]:
    m = OWNER_MARKER.match(title or "")
    if not m:
        return None
    name = (m.group("b") or m.group("c") or "").lower()
    return name if name in KNOWN else None      # "[platform]" is not an owner


def verdict(title: str, assignee: str, body: str = "") -> Optional[str]:
    """Pure decision function — unit-tested. Returns a refusal reason, or None."""
    a = (assignee or "").strip().lower()
    if not a or not (title or "").strip():
        return None                              # nothing to contradict
    if a not in KNOWN:
        return None                              # unknown names are core's job (it parks them)
    if OVERRIDE in (body or "").lower():
        return None                              # deliberate cross-lane, declared

    lane = _lane(title)
    marker = _marker_owner(title)

    if marker and marker != a:
        return (f"the title names {marker} as the owner but assignee is '{a}'")
    if lane and a in NON_LANE:
        why = {"axel": "axel is WeRoll-business-only and is not a builder",
               "switch": "switch is the switchboard and takes no board work",
               "brain": "brain has no kanban toolset and takes no cards"}[a]
        return (f"this is a {lane}-lane card and {why}")
    if lane == "build" and a in CROSS_LANE_BLOCK:
        return (f"this is a build-lane card but assignee is '{a}', who owns the "
                f"{'review' if a == 'rodge' else 'verify' if a == 'steve-o' else 'design'} lane")
    return None


def _message(title: str, assignee: str, reason: str) -> str:
    lane = _lane(title)
    expected = LANE_OWNER.get(lane or "", "the right lane owner")
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        f"  title:    {title[:120]}\n"
        f"  assignee: {assignee}\n"
        f"  expected: {expected}\n\n"
        "Mint-time routing is the defect that cost 2026-09-12 three mis-routed cards — "
        "a build card to rodge (57 minutes of heartbeats, no work), a coding card to axel "
        "(timed out at 602s), and an env card to axel again. In all three the card body "
        "said plainly who should own it.\n\n"
        "Re-mint with the correct assignee. If this really is a deliberate cross-lane "
        f"card, put '{OVERRIDE} <reason>' in the body and it will be allowed."
    )


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    try:
        if payload.get("tool_name") != "kanban_create":
            return None
        args = payload.get("args") or {}
        title = str(args.get("title") or "")
        assignee = str(args.get("assignee") or "")
        body = str(args.get("body") or "")
        reason = verdict(title, assignee, body)
        if not reason:
            return None
        logger.warning("kanban-mint-guard: refusing kanban_create — %s (title=%r assignee=%r)",
                       reason, title[:80], assignee)
        return {"action": "block", "message": _message(title, assignee, reason)}
    except Exception:  # noqa: BLE001
        # Never stop a board from minting because this guard had a bad day.
        logger.exception("kanban-mint-guard: unexpected error, allowing")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
