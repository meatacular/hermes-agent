"""Prevent ordinary minting of cards that can only be reviewed from review lane."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_REVIEWER_ASSIGNEES = frozenset({"rodge", "reviewer"})
_REVIEW_TITLE = re.compile(
    r"^\s*(?:\[rodge\]|rodge\s*[—–-]|re-?review\b|review\b)", re.I,
)


def review_lane_mint_reason(title: str, assignee: str) -> Optional[str]:
    """Return the routing defect, or None for ordinary cards."""
    if assignee.strip().lower() in _REVIEWER_ASSIGNEES and _REVIEW_TITLE.search(title):
        return "review-shaped card is being minted as an ordinary ready card"
    return None


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    try:
        if payload.get("tool_name") != "kanban_create":
            return None
        args = payload.get("args") or {}
        title = str(args.get("title") or "")
        assignee = str(args.get("assignee") or "")
        reason = review_lane_mint_reason(title, assignee)
        if not reason:
            return None
        logger.warning("review-lane-mint-guard: refusing ordinary review card title=%r", title[:100])
        return {
            "action": "block",
            "message": (
                "kanban_create rejected: this is a reviewer-shaped card, but ordinary minting "
                "starts it outside the review lane. Use the implementation card's "
                "kanban_request_review(reviewer='rodge') handoff instead; that records "
                "review_requested provenance and makes a later changes-requested verdict "
                "route back to the implementer."
            ),
        }
    except Exception:  # noqa: BLE001
        logger.exception("review-lane-mint-guard: unexpected error, allowing")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
