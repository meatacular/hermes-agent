"""Mint-time skill-availability guard, on upstream's ``pre_tool_call`` hook.

THE DEFECT (measured 2026-09-13)
--------------------------------
``build_preloaded_skills_prompt`` passes ``disabled_as_missing=True``: a skill listed in
a profile's ``skills.disabled`` CANNOT be force-loaded onto a card. But the dispatcher's
own pre-flight, ``hermes_cli.kanban_db.missing_skills_for``, only WALKS THE FILESYSTEM —
it has no idea the skill is disabled. Proven live:

    missing_skills_for('bob', ['ascii-art'])          -> []            (allow)
    build_preloaded_skills_prompt(['ascii-art'])      -> missing=[...] (worker dies)

So a card naming a disabled skill passes pre-flight, claims the card, builds the
workspace, forks the worker — and the worker exits 1 at agent init with
``Unknown skill(s)``. Twice, burning the whole retry budget, with the dispatcher seeing
only ``exit_code 1``. That is exactly the 2026-09-05 incident that lost six cards
(t_7f4ea155, t_0ec6abcf, t_867e31c9, t_03e5426e, t_8c40251d, t_34e858c9), re-armed on
2026-09-13 when ~55% of the fleet's skills were disabled as never-opened.

WHY A PLUGIN
------------
``hermes_cli/`` is upstream's substrate (2026-09-09 decision, enforced since 09-12).
extension-point: plugin. Imports NOTHING from hermes_cli — it reads the profile's
config.yaml directly, so it has no merge surface and cannot be broken by upstream moving
a symbol.

kind: backend so it loads in every profile AND every kanban worker — a user plugin in
~/.hermes/plugins is invisible to workers, which is why the completion gate was a silent
no-op from 09-09 to 09-11.

Contract (hermes_cli/plugins.py):
    {"action": "block", "message": "..."}  -> tool call refused
    None                                   -> allowed

Fail-open by construction: no skills named, no assignee, unreadable config, a skill that
is simply absent (the dispatcher's own check owns that case), or any exception at all ->
the card is created. A guard that guesses stops the board, which is worse than the defect.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["register", "on_pre_tool_call", "disabled_for", "verdict"]

logger = logging.getLogger(__name__)

GUARDED_TOOLS = frozenset({"kanban_create", "kanban_update"})
# Body escape hatch, same spirit as the mint guard's assignee-override:.
OVERRIDE = re.compile(r"skill-override:", re.I)


def _home() -> Path:
    h = os.environ.get("HERMES_HOME")
    if h:
        p = Path(h)
        # a profile-scoped home is <root>/profiles/<name>
        return p.parent.parent if p.parent.name == "profiles" else p
    return Path.home() / ".hermes"


def _config_path(assignee: str) -> Path:
    a = (assignee or "").strip()
    return _home() / "config.yaml" if a in ("", "default", "root") else _home() / "profiles" / a / "config.yaml"


def disabled_for(assignee: str) -> set:
    """The profile's ``skills.disabled`` set, or an empty set if it cannot be read.

    Deliberately a tiny hand-rolled scan rather than a yaml import: this module must not
    acquire a dependency the worker process might not have, and the shape is fixed
    (``skills:`` block, ``disabled:`` key, ``  - name`` items).
    """
    try:
        text = _config_path(assignee).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    lines = text.splitlines()
    try:
        si = next(i for i, l in enumerate(lines) if l.rstrip() == "skills:")
    except StopIteration:
        return set()
    out, in_disabled = set(), False
    for line in lines[si + 1:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break                                   # left the skills: block
        stripped = line.strip()
        if re.match(r"^disabled:\s*(\[\s*\])?\s*(#.*)?$", stripped):
            in_disabled = True
            continue
        if in_disabled:
            m = re.match(r"^-\s+(\S+)", stripped)
            if m:
                out.add(m.group(1).strip("\"'"))
                continue
            if stripped and not stripped.startswith("#"):
                in_disabled = False                 # a sibling key ended the list
    return out


def _installed(assignee: str, skill: str) -> bool:
    """Is the skill present on disk for this profile at all? (The dispatcher owns the
    absent case; we only want to fire on installed-but-disabled.)"""
    a = (assignee or "").strip()
    root = _home() / "skills" if a in ("", "default", "root") else _home() / "profiles" / a / "skills"
    try:
        return any(p.parent.name == skill for p in root.rglob("SKILL.md"))
    except OSError:
        return False


def verdict(args: Dict[str, Any]) -> Optional[str]:
    """Block message, or None. Pure: takes the tool arguments and nothing else."""
    skills = args.get("skills")
    if isinstance(skills, str):
        skills = [skills]
    if not skills or not isinstance(skills, (list, tuple)):
        return None
    assignee = (args.get("assignee") or "").strip()
    if not assignee:
        return None                                  # unassigned: the dispatcher parks it anyway
    if OVERRIDE.search(str(args.get("body") or "")):
        return None
    disabled = disabled_for(assignee)
    if not disabled:
        return None
    hit = [s for s in skills if isinstance(s, str) and s in disabled and _installed(assignee, s)]
    if not hit:
        return None
    return (
        f"Skill(s) disabled for profile {assignee!r}: {', '.join(sorted(hit))}. "
        f"They are installed but listed in that profile's skills.disabled, so "
        f"`--skills` treats them as MISSING and the worker would exit 1 at agent init — "
        f"twice, burning the card's whole retry budget, with the dispatcher seeing only "
        f"exit_code 1. The dispatcher's own pre-flight cannot catch this: "
        f"missing_skills_for() only walks the filesystem. "
        f"Fix: re-enable the skill in ~/.hermes/profiles/{assignee}/config.yaml "
        f"(skills.disabled), name a different skill, or put `skill-override:` in the body "
        f"with a reason if you really mean it."
    )


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    """Upstream calls this as ``invoke_hook("pre_tool_call", tool_name=..., args=...)``.

    The parameter is ``args`` — NOT ``tool_input``. Getting that wrong costs nothing
    visible: the hook still registers, still runs, reads ``None``, and returns ``None``,
    so the guard is a silent no-op and every unit test that calls it by the wrong keyword
    still passes. That is exactly how this plugin shipped green and inert on 2026-09-13,
    and why ``test_fires_through_upstreams_own_dispatcher`` exists below.
    """
    try:
        if payload.get("tool_name") not in GUARDED_TOOLS:
            return None
        args = payload.get("args")
        if not isinstance(args, dict):
            return None
        message = verdict(args)
        if not message:
            return None
        logger.warning("kanban-skill-guard: refusing %s — %s",
                       payload.get("tool_name"), message.split(".")[0])
        return {"action": "block", "message": message}
    except Exception:                                # noqa: BLE001 — fail open, always
        logger.debug("kanban-skill-guard failed open", exc_info=True)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
