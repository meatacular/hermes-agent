#!/usr/bin/env python3
"""escalation-watch — charter §6 escalation ladder, the human end (item 5).

Two things nothing else reports:

  1. **Escalation ceiling reached.** kanban-block-escalator (overwatch) spawns
     Agent Smith for a fault-signal block; on a hard stop — a second overwatch
     decision on one card, a cost breach after the one allowed extension, or a
     cap already at the hard ceiling — it leaves an `escalation-ceiling:`
     comment and spawns nobody. This watch tells Richie, once per
     (card, recurrence), with the block history as evidence.

     **The marker is the signal, not `block_kind` (fixed 2026-09-07).** This
     query used to exclude `block_kind='cost_cap'` on the assumption that
     `costcap-watch` covered those. It did not: the 09-06 policy change cut the
     allowed extensions from two to one, so the escalator now hard-stops at one
     while costcap-watch was still waiting for two — and a cost hard stop
     reached nobody at all. Proven on a scratch board: both watchdogs silent on
     the exact card the escalator leaves behind, while the same card as a
     non-cost block was reported. Zero ceiling markers exist in board history,
     so this had never fired and nothing would have shown it.

     `costcap-watch` now defers to this watch whenever a marker is present, so
     there is one report, not two.
  2. **Held cards.** Any `operator_hold` card older than 48 h gets a daily
     reminder — the hold is Richie's own pause, so the reminder is the whole
     action. (`fleet-preflight` separately turns RED on the same condition.)

Delivery: stdout -> cron `deliver` (photon iMessage); Slack DM via fleet_notify.
`no_agent`, read-only against kanban.db, zero LLM tokens, silent unless something
needs Richie. Exit 0 always.
Test overrides: ESCALATION_WATCH_DB, ESCALATION_WATCH_STATE, FLEET_NOTIFY_DRYRUN.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from fleet_notify import slack_dm
except Exception:  # noqa: BLE001
    def slack_dm(text, channel=None):  # type: ignore
        return False

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
DB = Path(os.environ.get("ESCALATION_WATCH_DB") or HERMES_HOME / "kanban.db")
STATE = Path(os.environ.get("ESCALATION_WATCH_STATE") or HERMES_HOME / "state" / "escalation-watch.json")

HOLD_REMIND_AFTER_H = 48
HOLD_REMIND_EVERY_H = 24
CEILING_MARKER = "escalation-ceiling"


def _history(c, tid):
    out = []
    try:
        for kind, payload, ts in c.execute(
            "SELECT kind, payload, created_at FROM task_events WHERE task_id=? "
            "AND kind IN ('blocked','unblocked','crashed','timed_out','gave_up','protocol_violation') "
            "ORDER BY id DESC LIMIT 8", (tid,)
        ).fetchall():
            reason = ""
            try:
                reason = (json.loads(payload or "{}").get("reason") or "")[:90]
            except Exception:  # noqa: BLE001
                pass
            out.append(f"    - {time.strftime('%m-%d %H:%M', time.localtime(ts))} {kind} {reason}")
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> int:
    if not DB.exists():
        return 0
    try:
        state = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        state = {}
    ceiling_seen = state.setdefault("ceiling", {})
    hold_seen = state.setdefault("hold", {})
    now = time.time()
    lines = []

    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
    except Exception as e:  # noqa: BLE001
        print(f"escalation-watch ERROR: {e}")
        return 0

    # 1. ceiling reached
    try:
        rows = c.execute(
            "SELECT t.id, t.title, t.assignee, t.block_kind, t.block_recurrences, "
            "  (SELECT body FROM task_comments WHERE task_id=t.id AND body LIKE ? ORDER BY id DESC LIMIT 1) AS marker "
            # No block_kind filter: the ceiling marker is written by the enforcer
            # at the moment it hard-stops, for cost and non-cost alike, and is the
            # only record of a decision that has actually been made. Filtering on
            # block_kind re-derives that decision from a field the platform does
            # not clear on resume — the same residual-field trap that silenced
            # stalled-card-watch.
            "FROM tasks t WHERE t.status IN ('blocked','triage')",
            (f"{CEILING_MARKER}%",),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        rows = []
        lines.append(f"  (ceiling query failed: {e})")
    for r in rows:
        if not r["marker"]:
            continue
        key = f"{r['id']}:{r['block_recurrences']}"
        if key in ceiling_seen:
            continue
        is_cost = (r["block_kind"] or "") == "cost_cap"
        kind_s = "cost cap" if is_cost else (r["block_kind"] or "fault signal")
        lines.append(
            f"*Escalation ceiling — your call Operator.* `{r['id']}` ({r['assignee'] or '?'}) "
            f"has blocked {r['block_recurrences']} times on {kind_s}; overwatch (Smith) has "
            f"hard-stopped it and spawned nobody. It stays blocked until you decide. "
            f"— {(r['title'] or '')[:80]}"
        )
        lines.append(f"  Marker: {r['marker'][:160]}")
        lines.append("  Block history:")
        lines.extend(_history(c, r["id"]))
        if is_cost:
            lines.append(
                "  Reply: `split <id>` to re-decompose it into fresh capped cards, "
                "`kill <id>`, or `raise <id> to $X` to override the $1.50 lifetime "
                "ceiling yourself — overwatch may not."
            )
        else:
            lines.append(
                "  Reply: `rewrite <id>` (Smith re-briefs it narrower), `kill <id>`, "
                "or `unblock <id>` to give it one more run."
            )
        ceiling_seen[key] = int(now)

    # 2. held > 48h, daily reminder
    try:
        holds = c.execute(
            "SELECT id, title, assignee, created_at, "
            "  (SELECT MAX(created_at) FROM task_events WHERE task_id=tasks.id AND kind='blocked') AS held_since "
            "FROM tasks WHERE status='blocked' AND block_kind='operator_hold'"
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        holds = []
        lines.append(f"  (hold query failed: {e})")
    old = []
    for r in holds:
        since = r["held_since"] or r["created_at"] or now
        age_h = (now - since) / 3600
        if age_h < HOLD_REMIND_AFTER_H:
            continue
        last = hold_seen.get(r["id"], 0)
        if now - last < HOLD_REMIND_EVERY_H * 3600:
            continue
        old.append(f"  - `{r['id']}` held {age_h/24:.1f} days — {(r['title'] or '')[:70]}")
        hold_seen[r["id"]] = int(now)
    if old:
        lines.append(f"*Held cards waiting on you* ({len(old)} older than {HOLD_REMIND_AFTER_H} h; daily reminder, and pre-flight is RED while they stand):")
        lines.extend(old)
        lines.append("  Release with `hermes kanban unblock <id>` or archive what you no longer want.")

    if not lines:
        return 0
    text = "\n".join(lines)
    print(text)
    slack_dm(text)
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
