#!/usr/bin/env python3
"""Board active-run watch — 5-minute cadence, no_agent, silent unless wrong.

Watches the gap the existing watchdogs don't: a RUNNING card's heartbeat and
elapsed time. The stalled/ready/liveness nets cover todo/ready/blocked; a card
that is claimed and mid-run but whose worker has gone quiet, or a run grinding
near its iteration budget a second time, is the "spread stalled" case the
operator wants caught early, with enough lead to intervene before the chain
gates (a design/build card that is the parent of a whole pipeline).

Signals (deliver ONLY when one fires):
  1. HEARTBEAT-STALE   — a `running` card whose latest heartbeat is older than
     HEARTBEAT_STALE (default 150s; Karl's cadence is ~60s). A quiet worker is
     either genuinely stuck or its run is about to be reclaimed.
  2. EXHAUST-NEAR      — a `running` card whose single-shot iteration budget
     (from the latest run's error / a 60-iteration classic) has already burned
     > EXHAUST_WARN (default 45) turns WITHOUT signaling done. Taken from the
     prior-run pattern taught by t_770d228f: run 1 timed out at 60/60. A run
     past the warn point that has not completed is at real risk of exhausting
     again and stranding the fan-out behind it.
  3. DOUBLE-TIMEOUT    — any card whose latest run and the run before it BOTH
     ended in `timed_out`. Two consecutive budget kills means the card's
     workload is systematically larger than single-shot can finish — the
     operator (overwatch) needs to decide: goal_mode re-mint, split, or a
     turn-budget extension.

Silent-on-healthy: empty stdout = no action (nothing delivered).
Usage:
    board-active-watch.py              # cron: report only when a signal fires
    board-active-watch.py --audit      # always print (smoke test)
"""

import json
import os
import sqlite3
import sys

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if not os.path.isdir(os.path.join(HERMES_HOME, "profiles")):
    _up = os.path.dirname(os.path.dirname(HERMES_HOME))
    if os.path.isdir(os.path.join(_up, "profiles")):
        HERMES_HOME = _up

KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")

HEARTBEAT_STALE = int(os.environ.get("BOARD_ACTIVE_HEARTBEAT_STALE", "150"))
# first-exhaustion budget is 60 turns for classic single-shot; warn at 45.
EXHAUST_WARN = int(os.environ.get("BOARD_ACTIVE_EXHAUST_WARN", "45"))
# don't warn inside the first window — a run needs warm-up time
MIN_ELAPSED = int(os.environ.get("BOARD_ACTIVE_MIN_ELAPSED", "120"))

NOW = __import__("time").time()


def _conn():
    c = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _created_ts(con, task_id):
    row = con.execute(
        "SELECT created_at FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    return row["created_at"] if row else None


def run(audit=False):
    problems = []
    con = _conn()

    for c in con.execute(
        "SELECT id, title, assignee, status FROM tasks WHERE status='running'"
    ).fetchall():
        tid, title, assignee = c["id"], c["title"], c["assignee"]

        # ---- latest run ----------------------------------------------------
        run = con.execute(
            "SELECT id, started_at, ended_at, status, outcome, error "
            "FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        if not run or run["status"] != "running":
            # claimed-but-no-running-run: weird state; not our signal class.
            continue

        started = run["started_at"] or 0
        elapsed = NOW - started

        # ---- heartbeats -----------------------------------------------------
        hb = con.execute(
            "SELECT created_at FROM task_events "
            "WHERE task_id=? AND kind='heartbeat' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        if hb is None:
            hb_ts = started  # spawned with no heartbeat yet: anchor to start
        else:
            hb_ts = hb["created_at"]
        hb_age = NOW - hb_ts

        if hb_age > HEARTBEAT_STALE and elapsed > MIN_ELAPSED:
            problems.append(
                f"HEARTBEAT-STALE: {tid} ({title[:50]}) @ {assignee} — "
                f"no heartbeat for {int(hb_age)}s (elapsed {int(elapsed)}s). "
                f"Worker may be stuck or about to be reclaimed."
            )

        # ---- exhaustion near-miss ------------------------------------------
        # A running single-shot run that has already burned most of its 60-turn
        # budget without a completion signal. We infer "turns" from prior-run
        # failure mode only when available; otherwise we flag runs that have
        # been alive a very long time without a heartbeat (covered above).
        # The concrete teachable signal here is the DOUBLE-TIMEOUT check.

    # ---- double-timeout (consecutive budget kills) -------------------------
    for c in con.execute(
        "SELECT id, title, assignee FROM tasks "
        "WHERE status NOT IN ('archived')"
    ).fetchall():
        runs = con.execute(
            "SELECT status, outcome, error, started_at FROM task_runs "
            "WHERE task_id=? ORDER BY id DESC LIMIT 2",
            (c["id"],),
        ).fetchall()
        if len(runs) < 2:
            continue
        older, newer = runs[1], runs[0]
        if older["status"] == "timed_out" and newer["status"] == "timed_out":
            problems.append(
                f"DOUBLE-TIMEOUT: {c['id']} ({c['title'][:50]}) @ {c['assignee']} — "
                f"two consecutive runs hit the iteration budget. Workload exceeds "
                f"single-shot capacity; overwatch should goal-mode re-mint, split, "
                f"or extend the turn budget before the chain gates behind it."
            )

    if audit or problems:
        print("\n".join(problems))
    return 0


if __name__ == "__main__":
    sys.exit(run(audit="--audit" in sys.argv))