"""Narrow plugin override for the dispatcher respawn guard."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)
_MARKER = "_respawn_guard_review_lane_wrapper"


def _is_review_or_rework(conn: Any, task_id: str) -> bool:
    row = conn.execute("SELECT assignee, skills FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return False
    assignee = row["assignee"] if hasattr(row, "keys") else row[0]
    raw_skills = row["skills"] if hasattr(row, "keys") else row[1]
    try:
        skills = json.loads(raw_skills) if isinstance(raw_skills, str) else raw_skills
    except (TypeError, ValueError):
        skills = []
    if assignee == "rodge" or (isinstance(skills, list) and "sdlc-review" in skills):
        return True
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'changes_requested' LIMIT 1",
        (task_id,),
    ).fetchone() is not None


def wrap_check_respawn_guard(original: Callable[..., Optional[str]]) -> Callable[..., Optional[str]]:
    if getattr(original, _MARKER, False):
        return original

    def wrapped(conn: Any, task_id: str, *, lane: str = "ready") -> Optional[str]:
        try:
            reason = original(conn, task_id, lane=lane)
        except Exception:
            logger.exception("respawn-guard-review-lane: wrapped predicate failed for %s", task_id)
            return "active_pr"
        if reason != "active_pr":
            return reason
        try:
            return None if _is_review_or_rework(conn, task_id) else reason
        except Exception:
            logger.exception("respawn-guard-review-lane: metadata probe failed for %s", task_id)
            return reason

    setattr(wrapped, _MARKER, True)
    return wrapped


def install() -> None:
    from hermes_cli import kanban_db_dispatch
    kanban_db_dispatch.check_respawn_guard = wrap_check_respawn_guard(
        kanban_db_dispatch.check_respawn_guard
    )


def register(ctx: Any) -> None:
    install()


__all__ = ["install", "register", "wrap_check_respawn_guard"]
