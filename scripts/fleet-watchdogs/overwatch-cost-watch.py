#!/usr/bin/env python3
"""overwatch-cost-watch — the one actor the card cap cannot see.

Why this exists
---------------
``enforce_max_cost`` only inspects cards that are ``running`` with a ``claim_lock``, and compares
the RUNNING ASSIGNEE'S ledger. Overwatch is neither: the escalator spawns it as a bare
``hermes chat -q`` under root, with no claim and no card of its own. It is therefore uncapped by
construction, and on 2026-09-14 it was the entire overspend:

    t_d423e2a5   root $3.111   (card cap $1.00, two spawns, NO cost_cap block ever fired)
    t_cff44897   root $1.838
    t_5262a7a8   root $1.099   (pooled $2.815 against a $1.50 ceiling)

$6.05 across three cards, none of it visible to the cap, while the three build/review workers on
those same cards were each inside their own $1. Richie's 2026-09-15 policy — "$1 per worker per
card, extendable to $1.50, after that they must be dealt with" — makes overwatch a worker for cap
purposes, and this is what measures it.

What it does
------------
For every non-terminal card, sum ROOT's spend on that card EXCLUDING cards root is the assignee of
(those are ordinary claimed workers and the kernel already caps them). Report past the base; past
the hard ceiling, terminate the overwatch session and say so on the card.

Silent when every overwatch session is inside its budget.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import sys
import time

H = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
if H.parent.name == "profiles":            # a profile home resolves to the fleet root, as the
    H = H.parent.parent                    # kernel's own get_default_hermes_root() does
KANBAN = H / "kanban.db"
ROOT_STATE = H / "state.db"
STATE = H / "state" / "overwatch-cost-watch.json"
BASE = 1.00
HARD = 1.50
TERMINAL = ("done", "archived")


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)


def root_spend_on(task_id: str) -> float:
    """Root's own ledger for sessions naming this card. Fail-open at 0.0."""
    if not ROOT_STATE.is_file():
        return 0.0
    try:
        c = _ro(ROOT_STATE)
        row = c.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM sessions WHERE title LIKE ?",
            ("%" + task_id + "%",),
        ).fetchone()
        return float(row[0] or 0.0)
    except sqlite3.Error:
        return 0.0
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def live_overwatch_pids(task_id: str) -> list:
    """PIDs of `chat -q` sessions naming this card. Best effort; never raises."""
    try:
        out = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        if task_id in line and "chat" in line and " -q" in line and "grep" not in line:
            m = re.match(r"\s*(\d+)\s", line)
            if m:
                pids.append(int(m.group(1)))
    return pids


def main() -> int:
    ap = argparse.ArgumentParser()
    # Trap 23: a cron `script` field takes NO ARGUMENTS — the scheduler looks up the whole string
    # as a file name and reports "Script not found". So the policy is the DEFAULT here and the
    # escape hatch is the flag, rather than the other way round. Richie 2026-09-15: past the hard
    # ceiling "they must be dealt with".
    ap.add_argument("--report-only", action="store_true",
                    help="do not terminate an overwatch session past the hard ceiling")
    ap.add_argument("--base", type=float, default=BASE)
    ap.add_argument("--hard", type=float, default=HARD)
    a = ap.parse_args()

    if not KANBAN.is_file():
        return 0
    try:
        k = _ro(KANBAN)
        cards = k.execute(
            "SELECT id, assignee, status, substr(title,1,60) FROM tasks "
            "WHERE status NOT IN (?, ?)", TERMINAL
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"OVERWATCH-COST-WATCH: board unreadable ({exc}) — treating as busy, no action")
        return 0

    over, ceiling = [], []
    for tid, assignee, status, title in cards:
        if (assignee or "") in ("default", "root"):
            continue                      # root IS the worker here; the kernel caps it
        spend = root_spend_on(tid)
        if spend > a.hard:
            ceiling.append((tid, assignee, status, title, spend))
        elif spend > a.base:
            over.append((tid, assignee, status, title, spend))

    if not over and not ceiling:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"at": int(time.time()), "fingerprint": []}))
        return 0

    # dedupe: only speak when the set of (card, bucket) changes
    fp = sorted([f"{t}:ceiling" for t, *_ in ceiling] + [f"{t}:over" for t, *_ in over])
    prev = []
    try:
        prev = json.loads(STATE.read_text()).get("fingerprint") or []
    except Exception:  # noqa: BLE001
        pass
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"at": int(time.time()), "fingerprint": fp}))
    if fp == prev:
        return 0

    lines = ["OVERWATCH COST — overwatch is a worker for cap purposes (Richie 2026-09-15)"]
    for tid, assignee, status, title, spend in ceiling:
        lines.append(f"  CEILING ${spend:.2f} > ${a.hard:.2f}  {tid} [{status}] assignee={assignee} — {title}")
        pids = live_overwatch_pids(tid)
        if pids and not a.report_only:
            for p in pids:
                try:
                    os.kill(p, signal.SIGTERM)
                    lines.append(f"    SIGTERM {p} (past the hard ceiling)")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"    could not signal {p}: {exc}")
        elif pids:
            lines.append(f"    live pid(s) {pids} — report only (--report-only was passed)")
        else:
            lines.append("    no live session — the spend is already banked; this is the record of it")
    for tid, assignee, status, title, spend in over:
        lines.append(f"  over    ${spend:.2f} > ${a.base:.2f}  {tid} [{status}] assignee={assignee} — {title}")
    lines.append("  Overwatch sessions are `chat -q` with no card claim, so enforce_max_cost cannot see them.")
    print("\n".join(lines), file=sys.stderr)
    print(json.dumps({"watchdog": "overwatch-cost-watch", "over": len(over), "ceiling": len(ceiling)}))
    return 0          # a finding is not a failure — exit 1 would grow failure_streak until the
                      # scheduler disabled this job, which is how a watchdog silences itself.


if __name__ == "__main__":
    sys.exit(main())
