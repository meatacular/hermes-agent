"""Route review-lane worktree cards to the implementation worktree.

The hook runs after a review claim commits and before workspace materialisation.
It is deliberately separate from kanban-worktree-carryover: this rule routes a
review card to its direct implementation parent; carry-over only recovers a
missing retry branch.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
HOOK = "kanban_task_claimed"


def _db_path(task_id: str, board: Optional[str]) -> Optional[Path]:
    override = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    candidates = [Path(override).expanduser()] if override else []
    home = Path(os.environ.get("HERMES_KANBAN_HOME") or (Path.home() / ".hermes"))
    slug = (board or "").strip() or "default"
    if slug != "default":
        candidates.append(home / "kanban" / "boards" / slug / "kanban.db")
    candidates.extend((home / "kanban.db", home / "kanban" / "kanban.db"))
    for path in candidates:
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                    return path
        except Exception:
            continue
    return None


def _review_parent(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    rows = conn.execute(
        "SELECT p.id, p.workspace_kind, p.workspace_path, p.branch_name "
        "FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.workspace_kind = 'worktree' "
        "ORDER BY p.id",
        (task_id,),
    ).fetchall()
    eligible = [row for row in rows if (row["workspace_path"] or "").strip() and (row["branch_name"] or "").strip()]
    return eligible[0] if len(eligible) == 1 else None


def _branch_present(workspace: str, branch: str) -> bool:
    """Require the reviewed branch to exist locally; never fetch on dispatch."""
    try:
        result = subprocess.run(
            ["git", "-C", workspace, "show-ref", "--verify", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def route_review(task_id: str, board: Optional[str] = None) -> dict[str, Any]:
    """Point a claimed review card at its sole eligible implementation parent.

    Returns a diagnostic verdict and fails open for every ambiguity or I/O
    failure. The dispatcher remains authoritative if this hook cannot decide.
    """
    verdict: dict[str, Any] = {"action": "noop", "reason": ""}
    db = _db_path(task_id, board)
    if db is None:
        verdict["reason"] = "task board unavailable"
        return verdict
    try:
        with sqlite3.connect(str(db), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute(
                "SELECT workspace_kind FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            claimed = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claimed' "
                "ORDER BY id DESC LIMIT 1", (task_id,)
            ).fetchone()
            if task is None or task["workspace_kind"] != "worktree":
                verdict["reason"] = "not a worktree card"
                return verdict
            payload = json.loads(claimed["payload"] or "{}") if claimed else {}
            if payload.get("source_status") != "review":
                verdict["reason"] = "not claimed from review"
                return verdict
            parent = _review_parent(conn, task_id)
            if parent is None:
                verdict["reason"] = "no sole eligible implementation parent"
                return verdict
            if not _branch_present(parent["workspace_path"], parent["branch_name"]):
                verdict["reason"] = "reviewed branch is missing locally"
                return verdict
            conn.execute(
                "UPDATE tasks SET workspace_path = ?, branch_name = ? WHERE id = ?",
                (parent["workspace_path"], parent["branch_name"], task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, strftime('%s','now'))",
                (task_id, "review_worktree_routed", json.dumps({
                    "workspace_path": parent["workspace_path"],
                    "branch_name": parent["branch_name"],
                    "parent_id": parent["id"],
                })),
            )
            conn.commit()
            verdict.update(action="routed", reason="review card routed to implementation worktree",
                           workspace_path=parent["workspace_path"], branch_name=parent["branch_name"],
                           parent_id=parent["id"])
            return verdict
    except Exception:
        logger.exception("kanban-review-worktree: failed open for task %s", task_id)
        verdict["reason"] = "exception while routing"
        return verdict


def on_task_claimed(task_id: Optional[str] = None, board: Optional[str] = None, **_kwargs: Any) -> None:
    try:
        if task_id:
            route_review(str(task_id), board)
    except Exception:
        logger.exception("kanban-review-worktree: unexpected error; claim left untouched")


def register(ctx: Any) -> None:
    ctx.register_hook(HOOK, on_task_claimed)


__all__ = ["register", "on_task_claimed", "route_review"]
