"""Review-round cap: the ceiling on `changes_requested` is enforced by CODE, not by a reviewer's mood.

See plugin.yaml for why this is a PLUGIN and not a core patch. Contract
(``hermes_cli/plugins.py``), the same one ``review-delta-guard`` and
``kanban-mint-guard`` use:

    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

THE DEFECT THIS CLOSES (card t_eb7a5c8c, 2026-09-16)
----------------------------------------------------
The review "round cap" was a CONVENTION, not a control. Nothing in ``hermes_cli/`` or
``tools/kanban_tools.py`` enforced it; ``sdlc-review`` set a *lens* per round, never a
ceiling. Measured on three cards in one day, the same unwritten rule produced three
different outcomes:

    t_d5cdd9f2  the reviewer escalated instead of bouncing (2 changes_requested on record)
    t_7540af48  two changes_requested, then a third round ran anyway (2 on record)
    t_b264ec8b  changes_requested, then the builder completed with no re-review (1 on record)

A rule that cannot be enforced cannot be waived consistently and cannot be tested. This
guard makes the ceiling a number in config, refuses the bounce past it in code, and makes
the bypass a RECORDED ACT (a comment on the card) rather than an unwritten style.

WHAT IS COUNTED — one variable, already in the record
-----------------------------------------------------
``round_now = bounces + 1``, where ``bounces`` is the number of ``task_runs`` rows on the
card with ``outcome = 'changes_requested'``. That is exactly how ``sdlc-review`` already
tells a reviewer which round they are in ("the current review round is that count plus
one"), so the guard counts what the reviewer counts — no second opinion, no new state.

    allowed_bounces = (max_review_rounds - 1) + authorised_extra_rounds

``max_review_rounds`` is the number of review ROUNDS a card gets, default 3. So the
default permits rounds 1, 2 and 3 (bounces 1 and 2) and refuses the THIRD bounce, which
would open round 4. A card sitting at exactly the cap is the refused case.

THE BYPASS IS A RECORDED ACT
----------------------------
A converging loop is authorisable, but only on the record: an overwatch authorisation is
its own comment whose FIRST non-blank line is

    review-round-extension: <who> +1 — <convergence evidence, closed per round>

and it grants exactly one further round, per authorisation. The marker must carry a real
grantor token and a count (the mint-guard lesson: a bare marker is switched off by any
body that merely quotes it). An unauthorised attempt is refused AND written to the card,
so the refusal is visible on the board and not only in the reviewer's head.

SCOPE — narrow on purpose, fail-OPEN everywhere
-----------------------------------------------
Only ``kanban_request_changes`` is touched, and only while the card is in an active
review round (``status == 'running'`` and a ``review_requested`` outcome on record). With
no such round the kernel's own, more precise refusal is the right error and this guard
stays silent. Any exception at all — an unreadable store, a malformed config, a missing
key — allows the call exactly as it behaves today: a guard that stops the review lane is
worse than the decorative cap it replaces.

Imports NOTHING from ``hermes_cli`` at import time (the two reads are narrow, lazy,
exception-guarded calls to the same helpers the kernel uses, which is what makes this
decision-exact rather than a second opinion).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "register", "on_pre_tool_call", "decision", "bounces_on_record", "grants_on_record",
    "configured_cap", "DEFAULT_MAX_REVIEW_ROUNDS", "GRANT_MARKER", "REFUSAL_PREFIX",
]

TOOL = "kanban_request_changes"

#: ``kanban.max_review_rounds`` — the number of review ROUNDS a card gets. The key is
#: declared on EVERY profile (the scoping law); this constant is the documented fallback
#: for a profile that has lost it, and it is deliberately the same number.
DEFAULT_MAX_REVIEW_ROUNDS = 3

#: The config key, as a dotted path.
CONFIG_KEY = "kanban.max_review_rounds"

#: A **whole comment** that authorises one further review round. First non-blank line,
#: marker first, then the grantor, then the count, then the convergence evidence.
GRANT_MARKER = "review-round-extension:"
GRANT_RE = re.compile(
    r"^review-round-extension:[ \t]*@?(?P<who>[A-Za-z0-9_.-]{1,64})[ \t]+(?P<n>\+?\d{1,2})\b",
    re.IGNORECASE,
)
#: ``<who>`` placeholders are illustrations, not grants: the guard's own refusal text
#: quotes the marker, and a quoted placeholder must never switch the cap off.
PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|x|n/?a|tbd|\.\.\.|…)$", re.I)
GRANT_MAX = 10  # an authorisation that says +99 is a typo, not a policy

#: The card-visible record of a refusal.
REFUSAL_PREFIX = "review-round-cap:"

# Nothing here shells out or opens a socket: two indexed SQLite reads and one small
# comment write. The bound exists because ``pre_tool_call`` fails CLOSED on its hook
# timeout, so this callback must never be the slow one.
_HOOK_BUDGET_S = 5


# --- the decisions (pure, so they can be tested without a board) -------------
def configured_cap() -> int:
    """``kanban.max_review_rounds`` for the CURRENT profile, or the documented default.

    Read through the same loader the kernel-adjacent plugins use, lazily and
    exception-guarded: a malformed config, an absent key or an import failure all mean
    "use the default", never "refuse everything".
    """
    try:
        from hermes_cli.config import load_config_readonly  # noqa: PLC0415

        cfg = load_config_readonly() or {}
        node: Any = cfg
        for part in CONFIG_KEY.split("."):
            if not isinstance(node, dict):
                return DEFAULT_MAX_REVIEW_ROUNDS
            node = node.get(part)
        if node is None:
            return DEFAULT_MAX_REVIEW_ROUNDS
        cap = int(str(node).strip())
        if cap < 1:
            logger.warning(
                "review-round-cap: %s=%r is below 1; clamping to 1 (a card with no "
                "rework round is a policy, not a typo to honour)", CONFIG_KEY, node,
            )
            return 1
        return cap
    except Exception:  # noqa: BLE001 -- fail OPEN, never refuse on our own error
        logger.debug("review-round-cap: config unreadable, using %s", CONFIG_KEY,
                     exc_info=True)
        return DEFAULT_MAX_REVIEW_ROUNDS


def _first_line(body: str) -> str:
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def grants_on_record(bodies: List[str]) -> Tuple[int, List[str]]:
    """``(extra_rounds, grantors)`` from the card's comments.

    Only the FIRST non-blank line of a comment counts, and only when it STARTS with the
    marker — a comment that merely mentions ``review-round-extension:`` (including this
    guard's own refusal text, which quotes it) authorises nothing.
    """
    total = 0
    who: List[str] = []
    for body in bodies or []:
        line = _first_line(body)
        m = GRANT_RE.match(line)
        if not m:
            continue
        if PLACEHOLDER.match(m.group("who")):
            continue
        n = int(m.group("n").lstrip("+") or 0)
        if n < 1:
            continue
        total += min(n, GRANT_MAX)
        who.append(m.group("who"))
    return total, who


def decision(*, cap: int, bounces: int, granted: int) -> Optional[str]:
    """``None`` when the bounce is allowed, else the one-line reason.

    ``cap`` counts ROUNDS, not bounces: round ``bounces + 1`` is the round in progress, and
    a bounce opens the next one. The bounce is refused once no round is left.
    """
    allowed_bounces = max(0, int(cap) - 1) + max(0, int(granted))
    if int(bounces) >= allowed_bounces:
        return (
            f"the card has used all {int(cap)} review round(s) "
            f"({int(bounces)} changes_requested on record, {int(granted)} extra round(s) "
            f"authorised by overwatch); refusing a bounce that would open round "
            f"{int(bounces) + 2}"
        )
    return None


# --- the board reads --------------------------------------------------------
def resolve_run(args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"id", "status", "bounces", "grant_bodies"}`` for a card in an active review
    round, or ``None`` (unknown card, not in review, unreadable store).

    The id comes from the call's own args, then the worker's ``HERMES_KANBAN_TASK`` — the
    same variable ``kanban_tools._default_task_id`` resolves from. A gateway or CLI caller
    can reach this tool with no worker environment at all, so an id is never assumed from
    the ambient environment alone.
    """
    tid = str(args.get("task_id") or "").strip() or str(
        os.environ.get("HERMES_KANBAN_TASK") or ""
    ).strip()
    if not tid:
        return None
    try:
        from hermes_cli import kanban_db_connect as kbc  # noqa: PLC0415

        with kbc.connect_closing() as conn:
            row = conn.execute(
                "SELECT id, status FROM tasks WHERE id = ?", (tid,),
            ).fetchone()
            if row is None:
                return None
            status = row["status"]
            # An active review round only. Otherwise the kernel's own error ("task is not
            # in an active review run") is the right one, and this guard must not mask it.
            if status != "running":
                return None
            reviewed = conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id = ? AND outcome = 'review_requested' "
                "LIMIT 1", (tid,),
            ).fetchone()
            if reviewed is None:
                return None
            bounces = conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE task_id = ? "
                "AND outcome = 'changes_requested'", (tid,),
            ).fetchone()[0]
            bodies = [
                r[0] for r in conn.execute(
                    "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,),
                ).fetchall()
            ]
            return {
                "id": str(row["id"]),
                "status": status,
                "bounces": int(bounces or 0),
                "grant_bodies": bodies,
            }
    except Exception:  # noqa: BLE001 -- fail OPEN, never block on our own error
        logger.debug("review-round-cap: card lookup failed, allowing", exc_info=True)
        return None


def record_refusal(task_id: str, body: str) -> bool:
    """Make the refusal VISIBLE ON THE CARD (AC2), via the kernel's own comment writer.

    Best-effort: a guard that cannot annotate the card still refuses the call — the
    refusal itself is the control, the comment is the evidence. De-duplicated against the
    card's most recent comment so a reviewer retrying the same bounce five times leaves
    one record, not five.
    """
    try:
        from hermes_cli import kanban_db as kb  # noqa: PLC0415
        from hermes_cli import kanban_db_connect as kbc  # noqa: PLC0415

        with kbc.connect_closing() as conn:
            last = conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if last is not None and str(last["body"] or "").strip() == body.strip():
                return False
            kb.add_comment(conn, task_id, "review-round-cap", body)
            return True
    except Exception:  # noqa: BLE001
        logger.debug("review-round-cap: could not record the refusal on %s", task_id,
                     exc_info=True)
        return False


# --- messages ---------------------------------------------------------------
def _message(card: Dict[str, Any], cap: int, bounces: int, granted: int) -> str:
    return (
        f"Refusing `kanban_request_changes` on {card['id']}: this card has used all "
        f"{cap} of its review rounds ({CONFIG_KEY} = {cap}).\n\n"
        f"  rounds used        : {bounces + 1} of {cap}\n"
        f"  changes_requested  : {bounces} on record\n"
        f"  extra rounds granted: {granted}\n\n"
        "The cap exists so a loop that is not closing gets ESCALATED instead of bounced "
        "forever. Two ways forward, both recorded — pick one:\n\n"
        "  1. ESCALATE. `kanban_block(kind=\"needs_input\")` on this card, naming the "
        "decision or prerequisite a human must supply. `needs_input` is in the block "
        "escalator's always-trigger set, so it spawns an overwatch session of Agent Smith "
        "on the FIRST block; a kind-less `kanban_block` is silently ignored the first "
        "time, which is the same as not escalating.\n\n"
        "  2. AUTHORISE ONE MORE ROUND — overwatch only, and it is an act, not a style. "
        "Post a NEW comment on this card whose first line is exactly:\n\n"
        f"       {GRANT_MARKER} <profile> +1 — <convergence evidence, closed per round>\n\n"
        "     e.g. `" + GRANT_MARKER + " default +1 — outstanding 11 -> 9 -> 5 across "
        "rounds 1-3`. One authorisation buys exactly one further round. The evidence is "
        "the point: the cap does not exist to stop a loop that is provably closing, it "
        "exists to stop one nobody is counting.\n\n"
        "Nothing was changed and nothing was counted: your review run is still open, and "
        "this refusal is not a failure, a block, or a bounce."
    )


def _comment(card: Dict[str, Any], cap: int, bounces: int, granted: int,
             grantors: List[str]) -> str:
    now_round = bounces + 1
    where = (f"review round {now_round} of {cap} is the last one"
             if now_round <= cap else
             f"the card is already past the cap (round {now_round} of {cap})")
    return (
        f"{REFUSAL_PREFIX} a `changes_requested` verdict was REFUSED by review-round-cap "
        f"— {where} "
        f"({CONFIG_KEY}={cap}; {bounces} changes_requested on record; {granted} extra "
        f"round(s) authorised{'; by ' + ', '.join(grantors) if grantors else ''}).\n\n"
        "The verdict did not land: the card was not bounced and the reviewing worker is "
        "still running. Escalation path: `kanban_block(kind=\"needs_input\")` -> overwatch "
        "(Agent Smith), or an overwatch authorisation posted as a new comment whose first "
        f"line is `{GRANT_MARKER} <profile> +1 — <convergence evidence>`."
    )


# --- the hook ---------------------------------------------------------------
def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, Any]]:
    try:
        if payload.get("tool_name") != TOOL:
            return None
        args = payload.get("args")
        args = args if isinstance(args, dict) else {}
        start = time.monotonic()
        card = resolve_run(args)
        if card is None:
            return None
        cap = configured_cap()
        granted, grantors = grants_on_record(card["grant_bodies"])
        reason = decision(cap=cap, bounces=card["bounces"], granted=granted)
        if reason is None:
            return None
        logger.warning("review-round-cap: refusing %s for %s — %s", TOOL, card["id"], reason)
        record_refusal(card["id"], _comment(card, cap, card["bounces"], granted, grantors))
        if time.monotonic() - start > _HOOK_BUDGET_S:
            logger.warning("review-round-cap: decision took %.1fs", time.monotonic() - start)
        return {"action": "block", "message": _message(card, cap, card["bounces"], granted)}
    except Exception:  # noqa: BLE001
        # Never stop a reviewer from returning work because this guard had a bad day.
        logger.exception("review-round-cap: unexpected error, allowing")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
