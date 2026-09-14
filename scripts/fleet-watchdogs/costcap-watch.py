#!/usr/bin/env python3
"""Cost-cap escalation watch - WeRoll cost policy (Richie, 2026-09-06).

The policy this enforces
------------------------
* Every card carries a flat **$1.00** cap. No agent writes dollar estimates;
  the old "estimate + 20%" rule and Steve-o's adjudication are both retired.
* A break blocks the card and spawns **Agent Smith as overwatch** — a fresh
  session with a $0 situation brief.
* Overwatch may extend **ONCE, by at most $0.50**, to a lifetime total of
  ``kanban.max_cost_hard_ceiling`` ($1.50).
* On a breach after that one extension — or a second overwatch decision on one
  card — the card stays blocked and Richie decides.

What changed on 2026-09-07, and why it mattered
-----------------------------------------------
``MAX_EXTENSIONS`` was still **2** here, a revision behind the enforcer: the
09-06 policy change cut the allowance to one, so ``kanban-block-escalator``
hard-stops after the first extension while this watch was still waiting for a
second. Meanwhile ``escalation-watch`` excluded ``block_kind='cost_cap'``
believing this watch covered it. **Neither did**: a card the escalator
hard-stopped for cost reached nobody. Proven on a scratch board — both watches
silent on the exact card the escalator leaves behind, the same card as a
non-cost block reported normally. Zero ceiling markers exist in board history,
so this had never fired and no alert could have revealed it.

Division of labour now:

* ``escalation-watch`` owns any card carrying an ``escalation-ceiling`` marker
  — that marker is the enforcer's own record of a decision it actually made.
* **This watch is the backstop**, for a cost breach that is exhausted but has
  NO marker, i.e. the escalator failed to record its hard stop. It skips marked
  cards so Richie gets one report, not two. A backstop that duplicates the
  primary teaches people to ignore both.

Extensions are counted from the audit trail rather than a new schema column:
every raise leaves a ``cost-extension`` comment on the card, and
``block_recurrences`` counts repeat blocks of the same kind. The higher of the
two is used, so a raise done without a comment still counts.

`no_agent`, read-only against kanban.db, zero LLM tokens. Silent unless a card
needs attention. Exit 0 always - a watch that crashes the scheduler is worse
than one that misses a tick.
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
STATE = HERMES_HOME / "state" / "costcap-watch.json"
LEDGER = HERMES_HOME / "logs" / "cost-ledger.jsonl"

HARD_CEILING = 1.50

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # 2026-09-11: say how much of a breach is ModelArk subscription (cap-equivalent) vs invoiced
    import billing_labels as BL
except Exception:  # noqa: BLE001 — never let a label helper silence a cost alert
    BL = None


def _split_line(tid):
    if BL is None:
        return None
    try:
        c = BL.card_costs(HERMES_HOME, {tid}).get(tid)
    except Exception:  # noqa: BLE001
        return None
    if not c or not c["modelark_calls"]:
        return None
    return (f"  Spend: ${c['billed']:.4f} billed + {BL.LABEL} ({c['modelark_calls']} calls — ModelArk reports "
            f"no cost; the cap counted them as ${c['modelark_notional']:.4f} cap-equivalent). Only the billed "
            f"figure is money.")
# One extension of <=$0.50, per Richie 2026-09-06. This MUST match the
# enforcer's limit in plugins/kanban-block-escalator (one EXTENSION_MARKER is a
# hard stop). When the two disagree, the gap between them is silent.
MAX_EXTENSIONS = 1
CEILING_MARKER = "escalation-ceiling"


def _cfg(key, fallback):
    try:
        import yaml

        cfg = yaml.safe_load((HERMES_HOME / "config.yaml").read_text()) or {}
        val = (cfg.get("kanban") or {}).get(key)
        return float(val) if val is not None else fallback
    except Exception:
        return fallback


def _extension_count(conn, tid: str, recurrences) -> int:
    """How many extensions this card has already been granted."""
    n = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE '%cost-extension%'",
            (tid,),
        ).fetchone()
        n = int(row[0]) if row else 0
    except Exception:
        n = 0
    try:
        rec = int(recurrences or 0)
    except Exception:
        rec = 0
    # block_recurrences counts repeat blocks; the first block is not an extension.
    return max(n, max(rec - 1, 0))


def _evidence(conn, tid: str) -> list:
    """Short supporting evidence for a Richie-facing escalation."""
    out = []
    try:
        rows = conn.execute(
            "SELECT profile, outcome, started_at, ended_at FROM task_runs "
            "WHERE task_id=? ORDER BY id DESC LIMIT 6",
            (tid,),
        ).fetchall()
        for r in rows:
            dur = (r[3] - r[2]) if (r[2] and r[3]) else 0
            out.append(f"    - {r[0]} {r[1]} ({dur}s)")
    except Exception:
        pass
    return out


def main() -> int:
    if not DB.exists():
        print(f"costcap-watch ERROR: kanban DB missing at {DB}")
        return 0

    hard = _cfg("max_cost_hard_ceiling", HARD_CEILING)
    ceiling = _cfg("max_cost_ceiling", 1.00)

    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    notified = state.get("notified", {})
    # Rising-edge flags for the policy-integrity checks: True once reported,
    # cleared back to False when the breach disappears, so a breach is
    # reported once and re-notified only after it clears and re-breaches.
    policy = state.get("policy", {})

    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        blocked = conn.execute(
            "SELECT id, title, assignee, max_cost, block_recurrences, tenant "
            "FROM tasks WHERE status='blocked' AND block_kind='cost_cap'"
        ).fetchall()
        uncapped_24h = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE max_cost IS NULL "
            "AND created_at > strftime('%s','now')-86400"
        ).fetchone()[0]
        over_ceiling = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE max_cost > ? "
            "AND status NOT IN ('done','archived')",
            (hard,),
        ).fetchone()[0]
    except Exception as e:
        print(f"costcap-watch ERROR: cannot read kanban DB: {e}")
        return 0

    lines = []

    # --- 1. BACKSTOP: overwatch's authority exhausted but no ceiling marker ---
    # escalation-watch owns every card that carries a marker, so this fires only
    # when the enforcer hard-stopped without recording it. Silence here is the
    # normal case; a report means the escalator itself failed.
    for r in blocked:
        tid = r["id"]
        exts = _extension_count(conn, tid, r["block_recurrences"])
        cap = r["max_cost"]
        cap_s = f"${cap:.2f}" if cap is not None else "(UNCAPPED - policy breach)"
        exhausted = exts >= MAX_EXTENSIONS or (cap is not None and cap >= hard)
        key = f"{tid}:{exts}"
        if not exhausted or key in notified:
            continue
        try:
            marked = conn.execute(
                "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE ? LIMIT 1",
                (tid, f"{CEILING_MARKER}%"),
            ).fetchone() is not None
        except Exception:  # noqa: BLE001
            marked = False  # fail open: better a duplicate than a silence
        if marked:
            continue  # escalation-watch reports this one
        lines.append(
            f"*Cost cap exhausted and UNRECORDED, your call Operator.* `{tid}` "
            f"({r['assignee'] or '?'}, tenant {r['tenant'] or 'weroll'}) "
            f"- {(r['title'] or '')[:90]}"
        )
        lines.append(
            f"  Cap {cap_s}; {exts} extension(s) granted against a maximum of "
            f"{MAX_EXTENSIONS} and a ${hard:.2f} lifetime ceiling — but overwatch "
            f"left NO `{CEILING_MARKER}` marker, so the escalator did not record a "
            f"hard stop. Treat the escalator as suspect as well as the card."
        )
        _sl = _split_line(tid)
        if _sl:
            lines.append(_sl)
        lines.append("  Recent runs:")
        lines.extend(_evidence(conn, tid))
        lines.append(
            "  Reply: `split <id>` to re-decompose it into fresh capped cards, "
            "`kill <id>` to leave it dead, or `raise <id> to $X` to override the "
            "ceiling yourself — overwatch may not."
        )
        notified[key] = int(time.time())

    # --- 2. policy-integrity checks (the silent-regression guard) -------------
    # Recommendation 2 from RUN-REPORT-2026-09-02-PM: phrased as "created in the
    # last 24h", NOT "open cards with no cap" - every uncapped card today was
    # already done, so an open-cards check would read zero and stay silent.
    # Notify once per breach, silence while it persists, re-notify only after
    # it clears and re-breaches (2026-09-07).
    if uncapped_24h and not policy.get("uncapped_24h"):
        lines.append(
            f"*Cost policy breach*: {uncapped_24h} card(s) created in the last 24h "
            f"with NO cap. Every creation path is supposed to apply one "
            f"(`effective_max_cost` in kanban_db.py). Check the deploy/decomposer "
            f"paths and whether the gateway is running the patched code."
        )
    if over_ceiling and not policy.get("over_ceiling"):
        lines.append(
            f"*Cost policy breach*: {over_ceiling} open card(s) capped above the "
            f"${hard:.2f} lifetime ceiling."
        )
    policy["uncapped_24h"] = bool(uncapped_24h)
    policy["over_ceiling"] = bool(over_ceiling)

    state["policy"] = policy
    state["notified"] = notified
    # Persist state even on a silent tick: the rising-edge policy flags must
    # track clear/re-breach. If a breach clears and we skip the write, an old
    # True flag would wrongly silence a later re-breach.
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE)
    except Exception:
        pass

    if not lines:
        return 0  # silent tick

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
