"""hold-marker-guard — a card minted as a human gate SAYS SO, in the place the reader reads.

The one-line contract this guard closes (card ``t_46783f69``, Richie-approved 3A+B,
2026-09-16):

    A card created as a human gate (`hold=True` -> `blocked` +
    `block_kind='operator_hold'`) must carry a line the watcher can see:

        operator-hold: manual

``scripts/release-operator-hold-watch.py`` reads that line from the card BODY (canonical:
"the minting card writes it") or from the newest ``blocked`` reason, and it is the ONLY
thing that keeps a hold (rule 2 ``manual_hold``, evaluated above every release rule).
Without it, an unmarked hold is RELEASED and announced by rule 3 (``no_parents``,
standalone hold) or rule 5 (``no_marker``, parents all done) — which is correct by design
since 2026-09-15: "the announcement is what replaced the gate".

The design assumed the minting card writes the marker. It did not. Measured on the live
board 2026-09-16, before this guard existed ("minted-held in 26h" census, re-run below):
four human-gate walls cut by one decomposer pass were released unmarked; every one of them
dispatched a worker that could not answer it, and three of those workers blocked again
with ``capability``/``needs_input`` — an ALWAYS-escalate kind — buying an overwatch session
per wall. The defect also fired on its own card: ``t_46783f69`` was minted held, released
under rule 3 fifteen seconds after filing, claimed, spawned, spent a worker run and ended
in a ``cost_cap`` block. One missing line, two budget events.

WHY A PLUGIN, AND WHY ITS OWN PIECE (the 3A+B ruling)
-----------------------------------------------------
The feasibility memo on ``t_4448a10d`` measured the reach of each seam: this hook sees
``kanban_create`` tool calls, which is 28 of the 46 held cards on the board (61%); the other
39% are created without a tool call (the auto-decomposer's direct INSERT in
``hermes_cli/kanban_db_graph.py``, ``hermes kanban create --hold``, the dashboard) and
``VALID_HOOKS`` has no creation-time hook, so no plugin can reach them. Richie therefore
ruled BOTH pieces: this plugin for the tool path, and the same rule at ``create_task`` /
the decomposer insert for the rest (shipped as apply-queue item 006, which owns the kernel
side). A plugin alone would read as "holds are handled" while a third of them were not —
the decorative-gate class.

WHY IT ADDS THE MARKER ONLY WHEN THE BODY CARRIES **NO** MARKER
--------------------------------------------------------------
A card whose body already declares ``operator-hold: dependency-wait`` was told, by its
author, that it is a sequencing wait. Adding ``manual`` would rewrite a wait that releases
itself into a sticky gate, which is the outcome Richie's 2026-09-15 rule exists to prevent
("I don't want any mandatory holds added back to the system"). So this guard speaks only
into silence. It also does not touch a body that already declares ``manual`` — the guard is
idempotent, and a second mint of the same card is a no-op.

ONE PARSER, PINNED TO THE READER
--------------------------------
The marker's syntax is defined by the READER, so the reader's parser is the authority and
``marker_of()`` uses it: the canonical helper added by the kernel piece
(``hermes_cli.hold_marker.find_marker``) when it is importable, and otherwise this module's
own copy — which is not a second opinion but the same line-anchored pattern, and which the
suite pins against the real ``release-operator-hold-watch.find_marker`` over a corpus of
bodies (mid-line prose, headings, list markers, emphasis, ``manualish``, ``manual-approval``,
the two-token case). A divergence between the two is a red test, not a silent release. That
is the drift protection the ruling asked for: after item 006 lands, both writers compute the
same predicate through one function.

FAIL-OPEN, AND THAT IS THE ONLY SAFE DIRECTION
----------------------------------------------
Refusing to mint is forbidden (it is how a board stops entirely, and the card says so), so
every failure — no args, a non-dict payload, a body that is not text, an unreadable
canonical module, any exception — returns ``None`` and the card is created exactly as it
would have been without this guard. Nothing here can stop a ``kanban_create``; the worst
case is the defect this guard closes, which is the state the board was already in.

kind: backend so it loads in every profile and every kanban worker — a user plugin in
``~/.hermes/plugins`` is invisible to workers, which is how the completion gate was a silent
no-op from 09-09 to 09-11.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "register", "on_pre_tool_call", "verdict", "marker_of", "find_marker",
    "mints_human_gate", "with_marker", "MARKER_LINE", "MARKER_MANUAL",
    "MARKER_DEPENDENCY",
]

logger = logging.getLogger(__name__)

_TOOL = "kanban_create"
_HOLD_KEY = "hold"

MARKER_MANUAL = "manual"
MARKER_DEPENDENCY = "dependency-wait"
MARKER_LINE = "operator-hold: manual"

# Mirrors ``tools.kanban_tools._BOOL_WORDS`` — the exact set the tool accepts for `hold`.
# A value outside it is refused by the tool itself, so anything unrecognised is treated as
# "not a hold" here and the card is minted unchanged.
_BOOL_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}

# Line-anchored, and copied verbatim from the reader's `_MARKER_RE`. The anchor IS the
# control: an unanchored search lets a card's own prose — "do NOT add `operator-hold:
# manual`" — arm (or arm-and-skip) a gate. `manual` must be a whole token (`\b`), so
# `manualish` does not match while `manual-approval` does.
_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+|>[ \t]*|#{1,6}[ \t]+)*"
    r"(?:\*\*|__|`)?[ \t]*operator-hold[ \t]*:[ \t]*(?:\*\*|__|`)?[ \t]*"
    r"(manual|dependency-wait)\b",
    re.IGNORECASE | re.MULTILINE,
)


def find_marker(*texts: Any) -> Optional[str]:
    """The marker a text declares, or None — same contract as the watcher's parser.

    ``manual`` wins over ``dependency-wait`` when both are present: the fail-safe direction
    is to keep the human gate.
    """
    found = set()
    for text in texts:
        if not text:
            continue
        for match in _MARKER_RE.finditer(str(text)):
            found.add(match.group(1).lower())
    if MARKER_MANUAL in found:
        return MARKER_MANUAL
    if MARKER_DEPENDENCY in found:
        return MARKER_DEPENDENCY
    return None


_CANONICAL: Optional[Tuple[Any, str]] = None


def canonical_parser() -> Tuple[Any, str]:
    """``(find_marker, where_it_came_from)`` — resolved once, lazily.

    Prefers the kernel helper the second half of 3A+B adds, so that from the moment item
    006 is applied there is ONE writer-side predicate in the fleet rather than two that
    agree by test. Before that, and on any import failure, this module's own copy is used
    and the suite is what keeps it identical to the reader's.
    """
    global _CANONICAL
    if _CANONICAL is None:
        try:
            from hermes_cli.hold_marker import find_marker as kernel_find_marker

            _CANONICAL = (kernel_find_marker, "hermes_cli.hold_marker")
        except Exception:                       # not applied yet, or unimportable here
            _CANONICAL = (find_marker, "hold-marker-guard")
    return _CANONICAL


def marker_of(*texts: Any) -> Optional[str]:
    """``find_marker`` through whichever parser is canonical in this process."""
    parser, _ = canonical_parser()
    return parser(*texts)


def mints_human_gate(args: Optional[Dict[str, Any]]) -> bool:
    """True when this ``kanban_create`` call creates a HELD card.

    ``hold`` is the tool's own gate flag: true means the card is born ``blocked`` with
    ``block_kind='operator_hold'`` (charter §5 — every job parent and every deploy card).
    It accepts a bool or the words in ``_BOOL_WORDS``.
    """
    if not isinstance(args, dict):
        return False
    value = args.get(_HOLD_KEY)
    if value is None or isinstance(value, bool):
        return bool(value)
    return _BOOL_WORDS.get(str(value).strip().lower(), False) is True


def with_marker(body: Any) -> str:
    """``body`` with the marker on its own line, appended. Idempotent by construction."""
    text = "" if body is None else str(body)
    if not text.strip():
        return MARKER_LINE + "\n"
    return text.rstrip("\n") + "\n\n" + MARKER_LINE + "\n"


def verdict(args: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The pure decision: the args to ADD, or None. Never raises.

    None means "mint exactly as asked" — the call is not held, or the body already says
    what kind of hold this is and this guard has no business overruling it.
    """
    if not isinstance(args, dict) or not mints_human_gate(args):
        return None
    body = args.get("body")
    if marker_of(body) is not None:
        return None
    return {"body": with_marker(body)}


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, Any]]:
    """``pre_tool_call`` hook: transform the mint's ``body`` so the hold survives.

    ``{"action": "modify", "args": {...}}`` shallow-merges its keys into the call's args
    (``hermes_cli/plugins.py``). Fail-open on everything else.
    """
    try:
        if payload.get("tool_name") != _TOOL:
            return None
        args = payload.get("args")
        if not isinstance(args, dict):
            return None
        add = verdict(args)
        if not add:
            return None
        return {"action": "modify", "args": add}
    except Exception:                                       # noqa: BLE001
        # Never stop a card being minted because this guard had a bad day.
        logger.exception("hold-marker-guard: unexpected error, minting unchanged")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
