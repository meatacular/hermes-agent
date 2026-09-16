#!/usr/bin/env python3
"""apply-queue — drain a queue of approved, self-contained fleet changes (2026-09-14).

Richie approves changes in conversation; this is the mechanism that lands them without a human
sitting through each deploy. It is a QUEUE DRAINER, not a change: to add work you drop a pair of
files into ``scripts/apply-queue/`` and arm it. The cron entry never changes.

    scripts/apply-queue/<id>.json   the descriptor (below)
    scripts/apply-queue/<id>.sh     a self-contained, IDEMPOTENT script

Descriptor::

    {"id": "001-thing", "title": "one line for the report",
     "armed": true,                  # false = parked; the drainer ignores it entirely
     "reason": "why parked",        # optional disarm explanation; why_disarmed/disarmed_why also accepted
     "script": "001-thing.sh",
     "requires_idle_board": true,    # default true
     "requires_clean_tree": false,   # true for anything that commits
     "restart": [],                  # profiles whose gateway must restart after; see NOTE
     "manifest": "applyq-001-20260914"}

Design rules, each of them paid for:

* **Armed is explicit.** A file appearing in the directory does nothing. Arming is the approval,
  and it is a separate act from writing the change.
* **ONE item per tick.** A failure cannot cascade into the next change, and every item re-checks
  the gates against a board that may have moved since the last one.
* **Fail-STOP, not fail-skip.** A failed item parks the whole queue until its `.failed` marker is
  cleared by hand. Order in a change queue is meaning, not decoration.
* **The kernel rule is enforced here too**, not just trusted: an item whose script touches
  ``hermes_cli/``, ``tools/``, ``agent/`` or ``gateway/`` is refused unless its descriptor carries
  ``core_patch_approved``. The 2026-09-12 decision is that policy lives in plugins, watchdogs,
  skills, SOULs and config; a queue that could quietly patch the substrate would be a way around it.
* **Idempotent by contract.** Every item script must be safe to run twice; the drainer's `.done`
  marker is a convenience, not the guard. A queue whose safety depends on a marker file is one
  `rm` away from re-running a migration.
* Silent when the queue is empty or fully drained — like every other watchdog here.

NOTE on ``restart``: the drainer never restarts a gateway itself. A gateway cannot restart itself
(L3) and the cron scheduler that runs this IS the thing that would die. It records what is owed and
says so; ``fleet-preflight``'s gateway-freshness check is what makes the debt visible, and the
restart-debt sweeper or an attended deploy pays it.

stdlib only: cron runs this under the SYSTEM python3, where ``import yaml`` raises.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
QUEUE = HERMES_HOME / "scripts" / "apply-queue"
STATE = HERMES_HOME / "state" / "apply-queue"
REPO = HERMES_HOME / "hermes-agent"
DB = HERMES_HOME / "kanban.db"
LOG = HERMES_HOME / "logs" / "apply-queue.log"

ITEM_TIMEOUT = int(os.environ.get("APPLY_QUEUE_TIMEOUT", "900"))   # < the scheduler's 3600s SIGKILL
KERNEL_DIRS = ("hermes_cli/", "tools/", "agent/", "gateway/")
# "In flight" means a card that can EXECUTE now: a busy status, or a parked card the kernel's
# own recompute_ready() could promote on the next tick (every dependency parent terminal, no
# sticky block). A `todo` card whose parents are not terminal cannot run at all, and counting it
# is how this gate went inert: on 2026-09-17 the board carried fifteen `todo` cards, every one of
# them waiting on an operator-held card, so every tick read "15 card(s) in flight" and no armed,
# approved item could ever land. Measured that day: old predicate 15, new predicate 0, and all
# fifteen losses are provably non-executing (each has a non-terminal parent).
# Emptiness is not quiescence — see board_busy().
BUSY = ("running", "ready", "in_progress")
BUSY_SQL = (
    "SELECT COUNT(*) FROM tasks t WHERE t.status IN ('running','ready','in_progress')"
    " OR (t.status IN ('todo','blocked')"
    "     AND coalesce(t.block_kind,'') <> 'operator_hold'"
    "     AND coalesce((SELECT e.kind FROM task_events e WHERE e.task_id = t.id"
    "                    AND e.kind IN ('blocked','unblocked')"
    "                   ORDER BY e.id DESC LIMIT 1),'') <> 'blocked'"
    "     AND NOT EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id"
    "                      WHERE l.child_id = t.id AND p.status NOT IN ('done','archived')))"
)
QUIET_MIN = float(os.environ.get("APPLY_QUEUE_QUIET_MIN", "10"))


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def board_busy():
    """(reason_not_idle, error). None reason = genuinely idle.

    TWO tests, because a zero count is not quiescence. The board reads 0 in the gap between one
    card archiving and its successor minting, so a count-only gate can fire in the middle of a
    live job — the bot-mode containment job of 2026-09-07 hit exactly this and grew the same
    second test. So: no card that can execute NOW (BUSY_SQL — a busy status, or a parked card the
    kernel could promote on the next tick) AND no board event at all for QUIET_MIN minutes.

    An unreadable board is treated as BUSY, never as idle: applying a change blind is the one
    outcome worse than applying it late.
    """
    if not DB.exists():
        return None, f"{DB} not found"
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        n = con.execute(BUSY_SQL).fetchone()[0]
        if n:
            con.close()
            return f"{n} card(s) in flight", None
        last = con.execute("SELECT MAX(created_at) FROM task_events").fetchone()[0]
        con.close()
    except sqlite3.Error as e:
        return None, str(e)
    if last:
        quiet = (time.time() - float(last)) / 60.0
        if quiet < QUIET_MIN:
            return f"board event {quiet:.0f} min ago (needs {QUIET_MIN:.0f} min quiet)", None
    return None, None


def git(*args):
    try:
        r = subprocess.run(["git", "--no-optional-locks", *args], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:                                  # noqa: BLE001
        return None


def touches_kernel(script_text):
    """A kernel path written into the item's own script. Deliberately crude and deliberately
    fail-CLOSED: a false positive costs one `core_patch_approved` line in the descriptor, a false
    negative costs an unreviewed patch to upstream's substrate."""
    hits = set()
    for line in script_text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for d in KERNEL_DIRS:
            if re.search(r"(?<![\w/])" + re.escape(d), line):
                hits.add(d.rstrip("/"))
    return sorted(hits)


def load_items():
    out = []
    if not QUEUE.is_dir():
        return out
    for j in sorted(QUEUE.glob("*.json")):
        try:
            d = json.loads(j.read_text())
        except ValueError as e:
            out.append({"id": j.stem, "_broken": f"unreadable descriptor: {e}"})
            continue
        d.setdefault("id", j.stem)
        d.setdefault("requires_idle_board", True)
        d.setdefault("requires_clean_tree", False)
        d.setdefault("restart", [])
        out.append(d)
    return out


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    items = load_items()
    if not items:
        return 0

    failed = sorted(p.stem for p in STATE.glob("*.failed"))
    if failed:
        # Fail-stop. Say it once per tick; the operator clears the marker when the item is fixed.
        print(f"apply-queue is PARKED: {len(failed)} item(s) failed and were never cleared — "
              f"{', '.join(failed)}. Nothing else will run until the .failed marker(s) in "
              f"{STATE} are removed. Detail: {LOG}")
        return 0

    pending = [d for d in items
               if not d.get("_broken")
               and d.get("armed") is True
               and not (STATE / f"{d['id']}.done").exists()]
    broken = [d for d in items if d.get("_broken")]
    for d in broken:
        print(f"apply-queue: {d['id']} — {d['_broken']}")
    if not pending:
        return 0

    item = pending[0]                                   # ONE per tick, in id order
    iid = item["id"]
    script = QUEUE / (item.get("script") or f"{iid}.sh")
    if not script.is_file():
        print(f"apply-queue: {iid} is armed but its script is missing ({script})")
        return 0

    # --- gates -------------------------------------------------------------
    text = script.read_text()
    kern = touches_kernel(text)
    if kern and not item.get("core_patch_approved"):
        print(f"apply-queue: REFUSED {iid} — its script touches upstream's kernel "
              f"({', '.join(kern)}) and the descriptor carries no `core_patch_approved`. "
              f"Policy (2026-09-12): plugin -> watchdog -> skill -> SOUL -> config; a core patch is "
              f"never the fallback. Park it or get it approved.")
        (STATE / f"{iid}.failed").write_text(json.dumps(
            {"at": int(time.time()), "reason": "kernel paths without approval", "paths": kern}))
        return 0

    if item["requires_idle_board"]:
        why, err = board_busy()
        if err:
            log(f"{iid}: deferred — board unreadable ({err})")
            return 0                                    # silent: not a fault, just not now
        if why:
            log(f"{iid}: deferred — {why}")
            return 0

    if item["requires_clean_tree"]:
        st = git("status", "--porcelain")
        if st is None:
            log(f"{iid}: deferred — cannot read the repo")
            return 0
        if st.strip():
            print(f"apply-queue: {iid} needs a clean tree and {REPO} is dirty "
                  f"({len(st.splitlines())} file(s)) — deferred, not failed.")
            return 0

    # --- run ---------------------------------------------------------------
    log(f"{iid}: running {script}")
    started = time.time()
    try:
        r = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True,
                           timeout=ITEM_TIMEOUT, cwd=str(HERMES_HOME))
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", f"timed out after {ITEM_TIMEOUT}s"
    took = time.time() - started
    tail = "\n".join((out + ("\n" + err if err else "")).strip().splitlines()[-25:])
    log(f"{iid}: rc={rc} in {took:.0f}s\n{tail}")

    rec = {"id": iid, "title": item.get("title", ""), "at": int(time.time()),
           "rc": rc, "seconds": round(took), "restart_owed": item.get("restart") or [],
           "manifest": item.get("manifest"), "output_tail": tail}
    if rc == 0:
        (STATE / f"{iid}.done").write_text(json.dumps(rec, indent=1))
        owed = rec["restart_owed"]
        print(f"apply-queue: APPLIED {iid} — {item.get('title', '')} ({took:.0f}s)"
              + (f"\n  Gateway restart owed: {', '.join(owed)}. The drainer never restarts a "
                 f"gateway itself (L3); pre-flight's gateway-freshness check now carries the debt."
                 if owed else "")
              + (f"\n  Rollback: fleet-rollback.sh {item['manifest']}" if item.get("manifest") else "")
              + (f"\n{tail}" if tail else ""))
    else:
        (STATE / f"{iid}.failed").write_text(json.dumps(rec, indent=1))
        print(f"apply-queue: FAILED {iid} — {item.get('title', '')} (rc={rc} after {took:.0f}s). "
              f"The queue is now PARKED; nothing further runs until "
              f"{STATE / (iid + '.failed')} is removed.\n{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
