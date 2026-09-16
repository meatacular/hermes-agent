"""Mint-time project-link guard, on upstream's ``pre_tool_call`` hook.

See plugin.yaml for why this is a PLUGIN and not the kernel change card t_331cb549
proposed (extension point 5 of 5, and the ladder's rule is that a core patch is not
the fallback).

Contract (``hermes_cli/plugins.py``):
    ``{"action": "block", "message": "..."}`` -> tool call refused
    ``None``                                   -> allowed

Imports NOTHING from ``hermes_cli`` at import time. The one store read is a narrow,
lazy, exception-guarded call to ``projects_db.get_project`` — the SAME single stable
function the kernel calls, which is what makes this guard decision-exact rather than
a second opinion. It is the same shape ``kanban-mint-guard`` already uses for
``profiles.profile_exists``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Iterable, Optional

__all__ = ["register", "on_pre_tool_call", "verdict", "explicit_project", "resolves"]

logger = logging.getLogger(__name__)

_TOOL = "kanban_create"


def explicit_project(args: Any) -> Optional[str]:
    """The caller's EXPLICIT project value, or ``None`` when they did not give one.

    Mirrors ``tools/kanban_tools.py`` line 887 exactly: ``args["project"]`` when the
    key is present, else ``args.get("project_id")``. The key's PRESENCE is the test,
    not its truthiness, because ``project=""`` is an explicit "no project" (#67567 /
    #106342) and ``project=None`` still takes the inherit path.

    ``None`` therefore means "the kernel may resolve this from the board or from the
    caller's own task" — the implicit path, which this guard never touches.
    """
    if not isinstance(args, dict):
        return None
    raw = args["project"] if "project" in args else args.get("project_id")
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def resolves(value: str) -> bool:
    """True when *value* names a project in the ACTIVE PROFILE's ``projects.db``.

    This is the whole of ``_resolve_project_link``'s lookup for the explicit case, and
    it is sufficient: on the tool path ``project_source_task_id`` is only ever set when
    the caller passed NO project at all (``kanban_tools._handle_create``), so an
    explicitly supplied value has no other way to resolve. A guard that disagreed with
    the kernel here would refuse cards the kernel accepts — which is worse than the
    defect it prevents.

    Fails OPEN: if the store cannot be read, the answer is "resolved" and the card is
    minted exactly as it is today.
    """
    try:
        from hermes_cli import projects_db as _pdb

        with _pdb.connect_closing() as conn:
            return _pdb.get_project(conn, value) is not None
    except Exception:  # noqa: BLE001 -- never a crash surface, never a false refusal
        logger.debug("kanban-project-link-guard: project store unreadable, allowing", exc_info=True)
        return True


def known_projects() -> list[str]:
    """``["p_5fe7127d (backupbrain)", ...]`` for the refusal message. ``[]`` on any error.

    The listing is the point of the refusal: a typo or a wrong-registry id becomes
    self-correcting when the caller is shown what the store actually holds.
    """
    try:
        from hermes_cli import projects_db as _pdb

        with _pdb.connect_closing() as conn:
            return [f"{p.id} ({p.slug})" for p in _pdb.list_projects(conn)]
    except Exception:  # noqa: BLE001
        return []


def _message(value: str, known: Iterable[str]) -> str:
    profile = (os.environ.get("HERMES_PROFILE") or "").strip()
    where = f"this profile's ({profile}) projects.db" if profile else "this profile's projects.db"
    listing = "\n".join(f"    {entry}" for entry in known) or "    (the store is empty)"
    return (
        f"Refusing to mint this card: project {value!r} is not in {where}.\n\n"
        "An explicit `project` that cannot be resolved used to be DROPPED SILENTLY and the\n"
        "card minted anyway — with no project link and a bare scratch workspace, so a typo\n"
        "or a wrong-registry id produced a repo-less card that looked normal on the board.\n"
        "The worker then opened in an empty directory. That is the defect; the refusal is\n"
        "the fix, because it is the one thing the old behaviour never did: tell you.\n\n"
        "Projects on this profile:\n"
        f"{listing}\n\n"
        "Proceed one of three ways:\n"
        "  * pass the right id or slug — `hermes project list` shows them;\n"
        "  * OMIT `project` entirely: the card then inherits the project from the board,\n"
        "    or from your own task when you are a dispatched worker;\n"
        "  * pass `project=\"\"` for a deliberate scratch card with no project at all.\n\n"
        "A project that exists only in ANOTHER profile's store cannot be linked from a\n"
        "cold start: the cross-profile fallback needs a project-linked worktree card as\n"
        "its source. Omit `project` and let the board supply it."
    )


def verdict(
    args: Any,
    *,
    resolve: Optional[Callable[[str], bool]] = None,
    listing: Optional[Callable[[], list[str]]] = None,
) -> Optional[str]:
    """The refusal message for these arguments, or ``None`` to allow. Pure modulo the
    injected ``resolve``/``listing``, so it is unit-tested without a projects.db."""
    value = explicit_project(args)
    if value is None:
        return None
    if (resolve or resolves)(value):
        return None
    return _message(value, (listing or known_projects)())


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    try:
        if payload.get("tool_name") != _TOOL:
            return None
        args = payload.get("args")
        message = verdict(args if isinstance(args, dict) else {})
        if not message:
            return None
        logger.warning("kanban-project-link-guard: refusing %s — project=%r did not resolve",
                       _TOOL, explicit_project(args))
        return {"action": "block", "message": message}
    except Exception:  # noqa: BLE001
        # Never stop a board from minting because this guard had a bad day.
        logger.exception("kanban-project-link-guard: unexpected error, allowing")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
