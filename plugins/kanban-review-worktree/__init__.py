"""Route review-shaped ``kanban_create`` calls to a reviewed worktree.

This is deliberately a mint-time rule.  Claim-time hooks cannot change the
already-materialised task handed to the dispatcher, while ``pre_tool_call``
can safely modify the arguments before the task is inserted.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
TOOL = "kanban_create"


def _db_path(board: Optional[str]) -> Optional[Path]:
    override = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    candidates = [Path(override).expanduser()] if override else []
    home = Path(
        os.environ.get("HERMES_KANBAN_HOME")
        or os.environ.get("HERMES_HOME")
        or (Path.home() / ".hermes")
    )
    if home.name != ".hermes" and home.parent.name == "profiles":
        home = home.parent.parent
    slug = (board or "").strip() or "default"
    if slug != "default":
        candidates.append(home / "kanban" / "boards" / slug / "kanban.db")
    candidates.extend((home / "kanban.db", home / "kanban" / "kanban.db"))
    for path in candidates:
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                conn.execute("SELECT 1 FROM tasks LIMIT 1")
            return path
        except Exception:
            continue
    return None


def _review_shaped(args: dict[str, Any]) -> bool:
    title = str(args.get("title") or "").strip().lower()
    return title.startswith(("review", "re-review", "re review", "[rodge]", "rodge —", "rodge -"))


def _branch_present(workspace: str, branch: str) -> bool:
    if not Path(workspace).is_dir() or not branch:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", workspace, "show-ref", "--verify", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _parent_workspace(board: Optional[str], parents: Any) -> tuple[Optional[dict[str, str]], str]:
    if not isinstance(parents, list) or len(parents) != 1 or not parents[0]:
        return None, "expected exactly one implementation parent"
    db = _db_path(board)
    if db is None:
        return None, "task board unavailable"
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks WHERE id = ?",
                (str(parents[0]),),
            ).fetchone()
        if row is None or row["workspace_kind"] != "worktree":
            return None, "implementation parent is not a worktree"
        workspace = (row["workspace_path"] or "").strip()
        branch = (row["branch_name"] or "").strip()
        if not workspace or not branch or not Path(workspace).is_dir():
            return None, "implementation worktree is missing"
        if not _branch_present(workspace, branch):
            return None, "reviewed branch is missing locally"
        return {"workspace_path": workspace, "branch_name": branch}, ""
    except Exception:
        logger.exception("kanban-review-worktree: unable to resolve implementation parent")
        return None, "exception while resolving implementation parent"


def verdict(args: Any) -> Optional[dict[str, Any]]:
    """Return modified mint arguments, or ``None`` to mint exactly as asked."""
    if not isinstance(args, dict) or not _review_shaped(args):
        return None
    if args.get("workspace_kind") is not None or args.get("workspace_path") is not None:
        return None
    parent, reason = _parent_workspace(args.get("board"), args.get("parents"))
    if parent is None:
        logger.info("kanban-review-worktree: mint left unchanged: %s", reason)
        return None
    return {"workspace_kind": "dir", "workspace_path": parent["workspace_path"]}


def on_pre_tool_call(**payload: Any) -> Optional[dict[str, Any]]:
    try:
        if payload.get("tool_name") != TOOL:
            return None
        args = payload.get("args")
        add = verdict(args)
        return {"action": "modify", "args": add} if add else None
    except Exception:
        logger.exception("kanban-review-worktree: mint failed open; card unchanged")
        return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)


__all__ = ["register", "on_pre_tool_call", "verdict"]