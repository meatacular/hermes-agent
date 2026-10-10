"""Per-worker card cost cap: `kanban.default_max_cost` each (3.00), one extension to
`kanban.max_cost_hard_ceiling` (4.50). Also renders the policy into worker prompts (1.1.0).

See plugin.yaml for why this is two wrappers and not a kernel change. The short version: the
kernel already measures the RUNNING ASSIGNEE'S own ledger, so the base allowance was already per
worker; what leaked was the extension (one number on the card, inherited by the next worker) and
the "once" test (counted per card, so a second worker could not be extended at all).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_MARK = "_per_worker_cap_wrapped"
EXT_PREFIX = "cost-extension:"
#: ``cost-extension: <assignee> $1.00 -> $1.50 by <who>``. The assignee token is what makes the
#: allowance personal; a comment without one is a pre-2026-09-15 extension and counts for nobody,
#: which is the safe direction (the worker gets the base, not someone else's raise).
EXT_RE = re.compile(r"^cost-extension:\s+@?(?P<who>[A-Za-z0-9_.-]+)\b")


def _base_cap(kdb) -> float:
    try:
        v = kdb.resolve_default_max_cost()
        return float(v) if v else 1.00
    except Exception:  # noqa: BLE001
        return 1.00


def _hard_cap(kdb) -> float:
    try:
        return float(kdb.resolve_max_cost_hard_ceiling())
    except Exception:  # noqa: BLE001
        return 1.50


def extended_for(conn, task_id: str, assignee: str) -> bool:
    """True iff THIS assignee has already had their one extension on THIS card."""
    who = (assignee or "").strip().lower()
    if not who:
        return False
    try:
        rows = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE 'cost-extension:%'",
            (task_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return False
    for (body,) in rows:
        m = EXT_RE.match((body or "").strip())
        if m and m.group("who").lower() == who:
            return True
    return False


def _normalise_caps(conn, kdb) -> list:
    """Set each running card's cap to what its CURRENT assignee is entitled to.

    Base for a worker who has not been extended; the hard ceiling for one who has. Returns the
    (task, old, new) tuples it changed, for the log.
    """
    base, hard = _base_cap(kdb), _hard_cap(kdb)
    changed = []
    rows = conn.execute(
        "SELECT id, assignee, max_cost FROM tasks "
        "WHERE status = 'running' AND max_cost IS NOT NULL"
    ).fetchall()
    for row in rows:
        tid = row[0] if not hasattr(row, "keys") else row["id"]
        who = row[1] if not hasattr(row, "keys") else row["assignee"]
        cur = row[2] if not hasattr(row, "keys") else row["max_cost"]
        want = hard if extended_for(conn, tid, who) else base
        try:
            cur_f = float(cur)
        except (TypeError, ValueError):
            continue
        # Never move a cap a human raised ABOVE the hard ceiling — only Richie can do that, and
        # if he has, this must not undo it.
        if cur_f > hard:
            continue
        if abs(cur_f - want) > 1e-9:
            conn.execute("UPDATE tasks SET max_cost = ? WHERE id = ?", (want, tid))
            changed.append((tid, cur_f, want, who))
    if changed:
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        for tid, old, new, who in changed:
            logger.info("per-worker cap: %s cap $%.2f -> $%.2f for assignee %s", tid, old, new, who)
    return changed


def _wrap_enforce(orig, kdb):
    def enforce_max_cost(conn, **kw):
        try:
            _normalise_caps(conn, kdb)
        except Exception as exc:  # noqa: BLE001 — a cap normaliser must never break the tick
            logger.warning("per-worker cap: normalise skipped (%s)", exc)
        return orig(conn, **kw)
    setattr(enforce_max_cost, _MARK, True)
    enforce_max_cost.__doc__ = (orig.__doc__ or "") + "\n\nWrapped by kanban-cost-cap-per-worker."
    return enforce_max_cost


def _wrap_set_cap(orig, kdb):
    def set_task_max_cost(conn, task_id, new_cap, *, by, reason=""):
        task = kdb.get_task(conn, task_id)
        who = (getattr(task, "assignee", None) or "") if task is not None else ""
        if task is not None and extended_for(conn, task_id, who):
            raise ValueError(
                f"{who or 'this worker'} has already been extended once on {task_id}; "
                "a second breach by the same worker stays blocked for Richie (overwatch rule)"
            )
        # The kernel refuses a second extension per CARD. Under the per-worker policy a different
        # worker is entitled to their own, so that check is bypassed here and re-applied above,
        # scoped to the assignee. Everything else the kernel does (hard ceiling, monotonicity,
        # the event, the audit comment) still runs.
        hard = _hard_cap(kdb)
        new_cap = float(new_cap)
        if new_cap > hard:
            raise ValueError(
                f"cap ${new_cap:.2f} exceeds the hard ceiling ${hard:.2f}; "
                "only Richie can move a worker past it (split the card instead)"
            )
        old = getattr(task, "max_cost", None) if task is not None else None
        if old is not None and new_cap <= float(old):
            raise ValueError(f"cap ${new_cap:.2f} is not above the current cap ${float(old):.2f}")
        with kdb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_cost = ? WHERE id = ?", (new_cap, task_id))
            kdb.add_comment(
                conn, task_id, author=by,
                body=(f"{EXT_PREFIX} {who or 'unassigned'} "
                      f"${float(old):.2f} -> ${new_cap:.2f} by {by}"
                      if old is not None else
                      f"{EXT_PREFIX} {who or 'unassigned'} (none) -> ${new_cap:.2f} by {by}")
                     + (f"\nreason: {reason}" if reason else ""),
            )
            kdb._append_event(conn, task_id, "cap_extended",
                              {"by": by, "for": who, "old": old, "new": new_cap, "reason": reason})
        logger.info("per-worker cap: %s extended to $%.2f for %s by %s", task_id, new_cap, who, by)
        return new_cap
    setattr(set_task_max_cost, _MARK, True)
    return set_task_max_cost


def install() -> list:
    """Idempotent. Returns the names wrapped on this call."""
    done = []
    try:
        from hermes_cli import kanban_db as kdb
    except Exception as exc:  # noqa: BLE001
        logger.warning("per-worker cap: kanban_db unavailable (%s) — cap behaviour unchanged", exc)
        return done
    if not getattr(kdb.enforce_max_cost, _MARK, False):
        kdb.enforce_max_cost = _wrap_enforce(kdb.enforce_max_cost, kdb)
        done.append("enforce_max_cost")
    if not getattr(kdb.set_task_max_cost, _MARK, False):
        kdb.set_task_max_cost = _wrap_set_cap(kdb.set_task_max_cost, kdb)
        done.append("set_task_max_cost")
    return done


# ── the policy, rendered (trim-a1-costcap-section-20261011) ─────────────────────────────────
# Until 2026-10-11 this policy lived as five hand-kept copies in the bob/jobsy/karl/rodge/steve-o
# SOULs, and all five said "$1.00, extend once by $0.50, Richie at $1.50" while the config had been
# 3.0 / 4.5 since the 2026-09-18 freeze — six agents asked on 2026-10-11 all quoted the stale figure.
# Rendering it HERE, from the same two resolvers enforce_max_cost and set_task_max_cost read under
# the worker's own HERMES_HOME, means the prompt cannot disagree with the control again.
SECTION_ID = "kanban-cost-cap"
WORKER_PROFILES = frozenset({"bob", "jobsy", "karl", "rodge", "steve-o"})
SECTION_MAX_CHARS = 2000


def policy_text(base: float, hard: float) -> str:
    return (
        "## Cost cap (rendered from the live cap control — this is the rule)\n"
        f"Each worker on a card has its own **${base:.2f}** allowance, measured on that worker's own "
        "spend. Passing it SIGTERMs your worker mid-run and blocks the card `kind=cost_cap`; work after "
        "that point is lost, so treat it as a real boundary. **Smith, as overwatch,** may extend a "
        f"worker **once**, to at most **${hard:.2f}**; a second breach by the same worker stays "
        "blocked and Richie decides.\n\n"
        "**No agent writes dollar estimates, and no agent sets or changes a cap** — not by editing "
        "`max_cost`, not by asking Smith. Points are the card author's, set at mint.\n\n"
        "**Work to the budget.** Prefer the cheap verification that answers the question. If mid-run "
        "you can see you will not finish inside it, **block and say why** — an honest block costs less "
        "than a SIGTERM and gives overwatch the evidence.\n\n"
        "**Resuming a card is not restarting it.** Read the card's comments first; Rodge's newest "
        "`outstanding-checklist` is the authoritative list of what is still missing."
    )


def render(session_info) -> str:
    """For the five workers, and for any process running a claimed card. "" on any failure —
    a half-stated cap is worse than none, and a render error must never cost a session."""
    try:
        import os
        prof = str((session_info or {}).get("profile_name") or "")
        if prof not in WORKER_PROFILES and not os.environ.get("HERMES_KANBAN_TASK"):
            return ""
        from hermes_cli import kanban_db as kdb
        base, hard = kdb.resolve_default_max_cost(), kdb.resolve_max_cost_hard_ceiling()
        if not base or not hard:
            logger.warning("per-worker cap: cap keys unresolved — cost-cap section not rendered")
            return ""
        return policy_text(float(base), float(hard))
    except Exception:  # noqa: BLE001
        logger.debug("per-worker cap: section render failed open", exc_info=True)
        return ""


def register(ctx) -> None:  # plugin loader entry point
    logger.info("kanban-cost-cap-per-worker 1.1: wrapped %s", install())
    try:
        ctx.register_system_prompt_section(SECTION_ID, render, position="after_memory",
                                           max_chars=SECTION_MAX_CHARS)
    except Exception as exc:  # noqa: BLE001 — the cap itself must still load
        logger.warning("per-worker cap: prompt section not registered (%s)", exc)
