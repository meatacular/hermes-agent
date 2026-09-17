"""Charter rule 3 as a PLUGIN, not a core patch.

Re-expresses ``tools/kanban_tools.py::_complete_uncommitted_work_rejection`` onto
upstream's ``pre_tool_call`` hook. See plugin.yaml for why this patch was chosen as
the proving run for the 2026-09-09 "stop patching core" decision.

Contract (hermes_cli/plugins.py):
    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

IMPORT DISCIPLINE — revised 2026-09-09 after reading the pinned upstream tree.
``kanban_db.connect`` is NOT defined in upstream's ``hermes_cli/kanban_db.py`` any
more; it is one of 1,148 names served by a temporary ``PLUGIN-COMPAT`` __getattr__
shim that ``COMPAT_MANIFEST.md`` says is **removed on 2026-09-14**, after which an
affected plugin is not loaded at all. So ``connect`` is imported from its new home
``hermes_cli.kanban_db_connect`` with a fallback to the old path for the current
(pre-merge) fleet tree. ``get_task`` is still genuinely defined in ``kanban_db``.

REPEAT ESCALATION — the core version recorded a ``completion_blocked_uncommitted_work``
event and escalated its message on a repeat refusal. The event's ONLY consumer was that
counter (verified: the marker is read nowhere outside ``kanban_tools.py``), and writing
it needs ``_append_event`` + ``write_txn`` — a private name that the compat layer
explicitly does NOT restore, plus one that has moved. So the counter is kept
**in-process** instead: a worker retrying ``kanban_complete`` in the same run is exactly
the case the escalation exists for. Cross-run counting and the board-visible forensic
trail are a named, accepted loss; the refusal is still logged to the gateway log.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

__all__ = ["register", "on_pre_tool_call"]

logger = logging.getLogger(__name__)

GIT_TIMEOUT_S = 10
MAX_SHOWN = 15

# task_id -> consecutive refusals in THIS process. Replaces the core version's
# task_events trail; see the module docstring for why.
_REFUSALS: Dict[str, int] = {}


def _kanban_api():
    """``(connect, get_task)`` from wherever they live in this tree.

    New path first: after upstream's Sep-2026 decomposition ``connect`` lives in
    ``hermes_cli.kanban_db_connect``. The old path still resolves today, but only
    through a shim with a 2026-09-14 delete date, and resolving through it emits a
    ``HermesPluginCompatWarning`` — so use the real home only.
    """
    # 2026-09-11: the ``kb.connect`` fallback is gone. It resolved through the
    # PLUGIN-COMPAT shim (deleted upstream 2026-09-14) in an import form upstream's
    # own scanner cannot see; ``kanban_db_connect`` exists on every tree we run.
    from hermes_cli import kanban_db as kb  # noqa: PLC0415
    from hermes_cli.kanban_db_connect import connect  # noqa: PLC0415
    return connect, kb.get_task


def _uncommitted_tracked(workspace: str) -> Optional[List[str]]:
    """Tracked-but-uncommitted paths in ``workspace``.

    Returns [] when clean, a list when dirty, and **None on any uncertainty** —
    no path, not a repo, git missing, timeout. None means ALLOW: the whole point
    of the gate is to stop work being lost, and refusing a completion we cannot
    reason about loses the run instead.
    """
    if not workspace or not os.path.isdir(workspace):
        return None
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=workspace, capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:          # not a repo, or git refused
        return None
    return [ln[3:].strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _message(dirty: List[str]) -> str:
    shown = "\n".join(f"  - {p}" for p in dirty[:MAX_SHOWN])
    more = f"\n  ...and {len(dirty) - MAX_SHOWN} more" if len(dirty) > MAX_SHOWN else ""
    return (
        "kanban_complete rejected: this card uses a `dir` workspace and has "
        f"{len(dirty)} TRACKED file(s) modified but NOT COMMITTED:\n"
        f"{shown}{more}\n\n"
        "A `dir` workspace is a SHARED working tree with no branch of its own. On "
        "2026-09-04 four cards were built, reviewed and marked done this way and the "
        "commit exists in no branch and no reflog — the next card clobbered the edits.\n\n"
        "Commit your work before reporting done:\n"
        "  git -C <workspace> add -A && git -C <workspace> commit -m '<what you did>'\n"
        "then call kanban_complete again with the commit SHA in your handoff. Your task "
        "is still in-flight; nothing was changed."
    )


def _repeat_message(dirty: List[str]) -> str:
    return (
        "kanban_complete rejected again: tracked edits are still uncommitted in this "
        f"`dir` workspace ({len(dirty)} file(s)). Do NOT keep retrying the completion — "
        "the run closing with this work uncommitted is precisely how it gets lost. If "
        "you cannot commit (no branch, wrong base, conflicting tree, missing identity), "
        "call kanban_block with the reason and the file list so it routes to someone who "
        "can. Blocking is safe; completing is not."
    )


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    """Block ``kanban_complete`` when a worker's `dir` workspace is dirty."""
    try:
        if payload.get("tool_name") != "kanban_complete":
            return None

        args = payload.get("args") or {}
        # Worker completions only. The orchestrator and the CLI pass through —
        # they are not the path that loses work. The card id comes from the
        # worker's environment: the hook payload's ``task_id`` is the AGENT's
        # effective task id (a fresh uuid4 for a `hermes chat -q` worker), never
        # the card id, so keying on it made this gate a silent no-op from
        # 2026-09-09 to 2026-09-11 (found by the catchup2 canary; fix manifest
        # gate-plugin-fix-20260911).
        task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
        if not task_id:
            return None
        arg_tid = args.get("task_id") or args.get("id") or ""
        if arg_tid and arg_tid != task_id:
            return None       # completing some other card: not this worker's workspace

        connect, get_task = _kanban_api()
        conn = connect()
        try:
            task = get_task(conn, task_id)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        if task is None:
            return None
        kind = getattr(task, "workspace_kind", None) or ""
        if kind != "dir":
            return None

        dirty = _uncommitted_tracked(getattr(task, "workspace_path", "") or "")
        if not dirty:                     # [] clean, or None uncertain -> allow
            _REFUSALS.pop(task_id, None)  # a clean pass resets the streak
            return None

        n = _REFUSALS.get(task_id, 0) + 1
        _REFUSALS[task_id] = n
        logger.warning(
            "kanban-completion-gate: refusing kanban_complete on %s (refusal %d in this "
            "run) — %d tracked file(s) uncommitted in %s", task_id, n, len(dirty),
            getattr(task, "workspace_path", "?"),
        )
        msg = _message(dirty) if n == 1 else _repeat_message(dirty)
        return {"action": "block", "message": msg}

    except Exception:  # noqa: BLE001
        # A gate must never crash a worker turn. Closing the run is exactly how the
        # work gets lost, which is the thing this exists to prevent.
        logger.exception("kanban-completion-gate: unexpected error, allowing")
        return None


def register(ctx) -> None:
    """Register the pre_tool_call gate."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
