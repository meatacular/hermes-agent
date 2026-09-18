"""Archive-time child-gating guard, on upstream's ``pre_tool_call`` hook.

See plugin.yaml for the incident this closes and why it is a plugin, not a core patch.

Contract (hermes_cli/plugins.py):
    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

Imports NOTHING from hermes_cli. It reads the board read-only and decides on the tool
arguments alone, so upstream moving a symbol cannot break it.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any, Dict, Optional

__all__ = ["register", "on_pre_tool_call", "verdict", "orphaned_children"]

logger = logging.getLogger(__name__)

ARCHIVE_TOOLS = frozenset({"kanban_archive", "kanban_archive_task", "archive_task"})
TERMINAL = frozenset({"done", "archived"})
SUPERSEDED_RE = re.compile(r"^\s*superseded-by:\s*(t_[0-9a-f]+)\s*$", re.M | re.I)


def _db_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".hermes", "kanban.db")


def orphaned_children(conn, card_id: str):
    """Children of `card_id` that are non-terminal and whose ONLY other parents are terminal.

    Those are exactly the cards `recompute_ready` would promote the instant this parent
    becomes `archived`, because it counts an archived parent as satisfied.
    """
    kids = [r[0] for r in conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ?", (card_id,))]
    if not kids:
        return []
    out = []
    for kid in kids:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (kid,)).fetchone()
        if not row or row[0] in TERMINAL:
            continue
        others = [r[0] for r in conn.execute(
            "SELECT p.status FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND l.parent_id != ?", (kid, card_id))]
        if all(s in TERMINAL for s in others):      # no gate left once we archive
            out.append(kid)
    return out


def verdict(card_id: str, reason: str = "", body: str = "", conn=None) -> Optional[str]:
    """None to allow, or the refusal text. Fail-open on every uncertainty."""
    if not card_id:
        return None
    if SUPERSEDED_RE.search(reason or "") or SUPERSEDED_RE.search(body or ""):
        return None                                  # the successor is named: archive is safe
    own = False
    try:
        if conn is None:
            path = _db_path()
            if not os.path.exists(path):
                return None
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            own = True
        kids = orphaned_children(conn, card_id)
    except Exception as exc:                         # never crash the archive path
        logger.debug("archive-guard standing down: %s", exc)
        return None
    finally:
        if own and conn is not None:
            try: conn.close()
            except Exception: pass
    if not kids:
        return None
    return (
        f"Refusing to archive {card_id}: it still gates {len(kids)} non-terminal "
        f"card(s) whose only other parents are already terminal — {', '.join(kids)}.\n\n"
        f"An ARCHIVED parent is read as SATISFIED by recompute_ready, so archiving this card "
        f"would promote those children immediately. That is correct for a parent that is DONE "
        f"and wrong for one that is SUPERSEDED. On 2026-09-18 it dispatched t_34dd83a9 against "
        f"the red base it existed to wait behind, and deadlocked the release it was waiting for.\n\n"
        f"Do one of these instead:\n"
        f"  1. re-point the children at the successor first "
        f"(`hermes kanban link <successor> <child>`), then archive; or\n"
        f"  2. say what supersedes this card — put `superseded-by: <card-id>` in the archive "
        f"reason or the card body, which is the act that makes the archive safe."
    )


def on_pre_tool_call(*args, **kwargs) -> Optional[Dict[str, Any]]:
    """Upstream dispatches ``invoke_hook("pre_tool_call", tool_name=..., args=...)``.

    The parameter names are NOT guessable and unit tests will happily confirm the wrong
    one — kanban-skill-guard shipped inert with 19 passing tests because it took
    `tool_input` (trap 43). So accept several shapes and fail open on anything unfamiliar.
    """
    tool = kwargs.get("tool_name") or kwargs.get("name") or (args[0] if args else None)
    if tool not in ARCHIVE_TOOLS:
        return None
    payload = (kwargs.get("args") or kwargs.get("arguments") or kwargs.get("tool_args")
               or kwargs.get("tool_input") or (args[1] if len(args) > 1 else None) or {})
    if not isinstance(payload, dict):
        return None
    ids = payload.get("task_id") or payload.get("task_ids") or payload.get("id")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, (list, tuple)):
        return None
    reason = payload.get("reason") or payload.get("note") or ""
    for cid in ids:
        if not isinstance(cid, str):
            continue
        body = ""
        try:
            path = _db_path()
            if os.path.exists(path):
                c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                row = c.execute("SELECT body FROM tasks WHERE id = ?", (cid,)).fetchone()
                body = (row[0] if row else "") or ""
                c.close()
        except Exception:
            body = ""
        msg = verdict(cid, reason=reason, body=body)
        if msg:
            return {"action": "block", "message": msg}
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
