#!/usr/bin/env python3
"""stalled-card-watch - a card that is alive but going nowhere.

Why this exists
---------------
2026-09-02, the v3.1 chain: Steve-o blocked `t_876ed1bf` at 17:29 with a
resumable, self-declared block ("the browser suite did not finish, I will not
fabricate a pass") and nothing picked it up again until 18:44 - **75 minutes of
dead time with a healthy dispatcher and a green board.**

`kanban-liveness-watch` covers the opposite failure: a dispatcher that has
stopped dispatching entirely. It is blind to this one, because the dispatcher
was fine - the card simply sat in a resumable state that nothing resumed.

What it reports (silent unless something is wrong):
  * blocked cards with a resumable/transient block and no run for >STALL_MIN
  * running cards whose worker heartbeat has gone cold
  * todo cards that are ready (no unmet dependency) but undispatched for too long
  * triage cards that are AUTO-DECOMPOSER-ELIGIBLE (created by a real user/
    profile, no decision-shaped intent requiring the PM, no operator_hold) but
    have sat unconsumed for >TRIAGE_STALL_MIN. This closes the 2026-09-04
    triage-stall: a spec'd+eligible card sat in triage 1.5h while the
    decomposer ticked every minute producing nothing - the decomposer's own
    WARNING escalation (Change B, kanban_watchers) catches repeated
    decompose-task failures, and this check catches the card-level stall the
    decomposer never reached because it was failing on another card or not
    running at all.

Breach-only reporting (2026-09-07, Richie): a card is reported ONCE when its
stall is first detected, then stays silent on subsequent ticks while the same
stall persists. It is forgotten once the stall clears, so a card that clears
and later re-stalls is reported again. State lives in
~/.hermes/state/stalled-card-watch.json.

`no_agent`, read-only, zero LLM tokens. Exit 0 always.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
DB = HERMES_HOME / "kanban.db"
# Rising-edge dedup state: notify once per (kind, card) breach, silence while
# it persists, re-notify only after it clears and re-breaches.
STATE = HERMES_HOME / "state" / "stalled-card-watch.json"

STALL_MIN = int(os.environ.get("STALL_MIN", "30"))          # resumable block idle
READY_STALL_MIN = int(os.environ.get("READY_STALL_MIN", "45"))  # ready but undispatched
HEARTBEAT_STALE_MIN = 20
# Triage card that is decomposer-eligible but unconsumed for this long is a
# silent stall (2026-09-04: a spec'd card sat in triage ~90 min while the
# decomposer ticked every minute). 30 min gives an auto-decompose burst (3/tick)
# plenty of headroom to reach it, and any genuine card waiting that long is
# almost certainly stuck.
TRIAGE_STALL_MIN = int(os.environ.get("TRIAGE_STALL_MIN", "30"))

# Blocks a human must clear are NOT stalls - they are correctly waiting.
TERMINAL_BLOCKS = {"operator_hold", "needs_input", "cost_cap", "capability"}


def _mins(ts):
    return (time.time() - ts) / 60.0 if ts else None


def _read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _write_state(state):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE)
    except Exception:
        pass



def _has_loop_break_event(conn, task_id: str) -> bool:
    """True when the block-loop breaker actually parked this card.

    Reads the event, not ``block_kind``: the column survives an unblock by
    design, so it cannot answer "is this card parked right now?".
    Fail-open — an unreadable event log must not silence a stall report.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='block_loop_detected' LIMIT 1",
            (task_id,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def main() -> int:
    if not DB.exists():
        return 0
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
    except Exception as e:
        print(f"stalled-card-watch ERROR: {e}")
        return 0

    # Rising-edge dedup: a breach key is reported once when it first appears;
    # it stays silenced while still present and is forgotten once it clears so
    # a later re-breach is reported again. Keys are (kind, id), e.g.
    # "stalled:t_1a2b3c4d", plus a single "errors" key for any check failure.
    state = _read_state()
    cur_keys = set(state.get("reported", []))
    out = []
    new_keys = []

    def _emit(key: str, line: str) -> None:
        if key not in cur_keys:
            out.append(line)
        new_keys.append(key)

    try:
        rows = c.execute(
            "SELECT id,title,assignee,block_kind,status FROM tasks WHERE status='blocked'"
        ).fetchall()
        for r in rows:
            if (r["block_kind"] or "") in TERMINAL_BLOCKS:
                continue  # waiting on a human, by design
            last = c.execute(
                "SELECT MAX(COALESCE(ended_at,started_at)) FROM task_runs WHERE task_id=?",
                (r["id"],),
            ).fetchone()[0]
            idle = _mins(last)
            if idle is not None and idle > STALL_MIN:
                _emit(
                    f"stalled:{r['id']}",
                    f"  STALLED {r['id']} [{r['assignee'] or '-'}] blocked "
                    f"kind={r['block_kind'] or 'none'} - no run for {idle:.0f} min "
                    f"- {(r['title'] or '')[:60]}",
                )
    except Exception as e:
        _emit("errors", f"  (blocked-card check failed: {e})")

    try:
        for r in c.execute(
            "SELECT id,title,assignee,last_heartbeat_at FROM tasks WHERE status='running'"
        ).fetchall():
            idle = _mins(r["last_heartbeat_at"])
            if idle is not None and idle > HEARTBEAT_STALE_MIN:
                _emit(
                    f"cold:{r['id']}",
                    f"  COLD    {r['id']} [{r['assignee'] or '-'}] running but worker "
                    f"heartbeat is {idle:.0f} min old - {(r['title'] or '')[:60]}",
                )
    except Exception:
        pass

    try:
        for r in c.execute(
            # runfix-20260914: 'ready' added. A card the dispatcher REFUSES to spawn (respawn_guarded)
            # sits in 'ready', not 'todo', so this arm could not see it at any threshold —
            # measured 2026-09-13, t_22502d22, 24 refusals over 21 min, reported by nobody.
            "SELECT t.id,t.title,t.assignee,t.created_at FROM tasks t WHERE t.status IN ('todo','ready') "
            "AND NOT EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id "
            "                WHERE l.child_id=t.id AND p.status NOT IN ('done','archived'))"
        ).fetchall():
            idle = _mins(r["created_at"])
            if idle is not None and idle > READY_STALL_MIN:
                _emit(
                    f"undisp:{r['id']}",
                    f"  UNDISP  {r['id']} [{r['assignee'] or '-'}] ready but undispatched "
                    f"for {idle:.0f} min - {(r['title'] or '')[:60]}",
                )
    except Exception:
        pass

    try:
        # Triage cards that the auto-decomposer SHOULD be able to consume but
        # hasn't. Mirrors kanban_decompose.list_triage_ids() eligibility:
        # exclude cards the decomposer itself parked (created_by auto-decomposer
        # or "decomposer" - those are decision-shaped children awaiting the PM,
        # by design), and exclude block_loop breakers (routed to triage for a
        # human decision, not for decomposition; BLOCK_RECURRENCE_LIMIT=2).
        blocking = c.execute(
            "SELECT id,title,assignee,created_at,created_by,block_kind,block_recurrences "
            "FROM tasks WHERE status='triage'"
        ).fetchall()
        for r in blocking:
            created_by = (r["created_by"] or "")
            if created_by in ("auto-decomposer", "decomposer"):
                continue  # parked as a PM decision by design
            # 2026-09-07: block_kind is HISTORY on a triage card, not state.
            # The platform deliberately preserves it after an unblock ("only
            # complete_task clears them"), so the two skips that used to live
            # here were false negatives: a card whose stale kind happened to be
            # operator_hold was skipped as "waiting on a human" (a card actually
            # waiting on a human is status='blocked', not 'triage'), and any card
            # ever blocked twice was skipped forever as a loop breaker, long
            # after that block was resolved. Both silenced exactly the stalls
            # this watchdog exists to notice.
            #
            # The loop-breaker signal is the EVENT, which is a fact about what
            # happened rather than a field that outlives its meaning.
            if _has_loop_break_event(c, r["id"]):
                continue  # genuinely parked for a human by the loop breaker
            idle = _mins(r["created_at"])
            if idle is not None and idle > TRIAGE_STALL_MIN:
                _emit(
                    f"triage:{r['id']}",
                    f"  TRIAGE-STALL {r['id']} [{r['assignee'] or '-'}] "
                    f"decomposer-eligible but unconsumed for {idle:.0f} min - "
                    f"{(r['title'] or '')[:60]}",
                )
    except Exception as e:
        _emit("errors", f"  (triage-eligibility check failed: {e})")

    if out:
        print("stalled-card-watch - cards alive but going nowhere:")
        print("\n".join(out))
        print("  (a resumable block with no runs usually means the promoter is not "
              "re-queuing it; check the dispatcher log and the card's parents)")
    # Persist the CURRENT breach set so a breach already reported stays silent
    # on subsequent ticks. Keys that are no longer present vanish, so a card
    # that clears and later re-stalls is reported again.
    _write_state({"reported": sorted(new_keys)})
    return 0


if __name__ == "__main__":
    sys.exit(main())