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
# 2026-09-16 (Richie): "delay deployment of this work to later tonight … structure so it can be
# unattended … ensure rollback are used so error risks can be mitigated … more work may be added to
# the cron across the day." So the drainer keeps its 15-minute tick — a short tick is what lets an
# urgent item land promptly and what keeps each item re-checking a board that may have moved — but
# it only DEPLOYS inside a night window. Work queued through the day accumulates and lands while
# nobody is watching, which is also when the board is quiet and the providers are off-peak.
#
# Local time, deliberately: the Mac runs NZ time, `time.localtime()` needs no tz library, and this
# file is stdlib-only because cron runs it under the system python3 where `import yaml` raises.
WINDOW_OPEN_H = int(os.environ.get("APPLY_QUEUE_OPEN_H", "22"))    # 22:30 local
WINDOW_OPEN_M = int(os.environ.get("APPLY_QUEUE_OPEN_M", "30"))
WINDOW_CLOSE_H = int(os.environ.get("APPLY_QUEUE_CLOSE_H", "5"))   # 05:30 local
WINDOW_CLOSE_M = int(os.environ.get("APPLY_QUEUE_CLOSE_M", "30"))
# An item may carry "urgent": true to bypass the window. It is meant for a fix that should not wait
# a whole day, and it is deliberately a per-item opt-in rather than a global switch.
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

# 2026-09-17 (Richie, Phase 0 of BOARD-REVIEW-AND-PLAN-2026-09-16): a kernel item lands only when
# (1) its source card is `done` and passed a REVIEW — the last review_requested run was followed by a
# `completed` run from a different profile, with no changes_requested after it — and (2) Richie's
# approval is on the board in his own voice: `core_patch_approval_comment` names a task_comments row
# authored from his channels. `core_patch_approved: true` alone is written by the worker that built
# the patch, so on its own it certifies nothing. On 2026-09-16 items 006 and 009 were armed with it
# and neither had ever passed review; 007 was armed while its build card was still running.
APPROVER_AUTHORS = ("desktop", "dashboard")


def kernel_item_cleared(item):
    """(ok, reason). Fail CLOSED: an unreadable board or a missing field means not cleared."""
    card = item.get("card")
    if not card:
        return False, "names no source `card`, so its review cannot be checked"
    appr = item.get("core_patch_approval_comment")
    try:
        appr = int(appr)
    except (TypeError, ValueError):
        return False, ("carries no `core_patch_approval_comment` (the task_comments id of Richie's "
                       "approval on the board)")
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT status FROM tasks WHERE id = ?", (card,)).fetchone()
            runs = con.execute("SELECT profile, outcome FROM task_runs WHERE task_id = ? "
                               "AND outcome IN ('review_requested','changes_requested','completed') "
                               "ORDER BY id", (card,)).fetchall()
            who = con.execute("SELECT author FROM task_comments WHERE id = ?", (appr,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return False, f"board unreadable ({e})"
    if not row:
        return False, f"source card {card} does not exist"
    if row[0] != "done":
        return False, f"source card {card} is `{row[0]}`, not done"
    last_rr = max((i for i, r in enumerate(runs) if r[1] == "review_requested"), default=None)
    if last_rr is None:
        return False, f"source card {card} never went through review"
    after = runs[last_rr + 1:]
    if any(r[1] == "changes_requested" for r in after):
        return False, f"source card {card}'s last review requested changes"
    if not any(r[1] == "completed" and r[0] != runs[last_rr][0] for r in after):
        return False, f"source card {card} has no approving review by a different profile"
    if not who or who[0] not in APPROVER_AUTHORS:
        return False, (f"approval comment {appr} is not Richie's (author "
                       f"{who[0] if who else 'missing'!r}, needs one of {', '.join(APPROVER_AUTHORS)})")
    return True, "reviewed and approved"


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


def in_window(now=None) -> bool:
    """True inside the night deploy window. Handles the wrap over midnight."""
    t = time.localtime(now if now is not None else time.time())
    mins = t.tm_hour * 60 + t.tm_min
    o = WINDOW_OPEN_H * 60 + WINDOW_OPEN_M
    c = WINDOW_CLOSE_H * 60 + WINDOW_CLOSE_M
    return (mins >= o or mins < c) if o > c else (o <= mins < c)


def rollback(manifest: str) -> tuple:
    """Reverse an item's change manifest. Returns (ok, one-line summary).

    Why the drainer does this rather than printing the command: an unattended run that fails at
    02:00 and leaves the change half-applied is exactly the risk this window creates. The manifest
    already holds a pre-image per file and a git pre/post per repo, so the reversal is mechanical —
    what was missing was anything CALLING it. `--no-restart` because the drainer never signals a
    gateway (L3, and the cron running this is what would die); the restart debt stays visible to
    pre-flight either way.
    """
    if not manifest:
        return False, "no manifest declared — nothing to roll back automatically"
    script = HERMES_HOME / "scripts" / "change-manifest.py"
    if not script.is_file():
        return False, f"change-manifest.py not found at {script}"
    try:
        r = subprocess.run([sys.executable, str(script), "rollback", manifest, "--no-restart"],
                           capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        return False, f"rollback raised: {exc}"
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-8:])
    return r.returncode == 0, tail or f"rc={r.returncode}"


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    items = load_items()
    if not items:
        return 0

    failed = sorted(p.stem for p in STATE.glob("*.failed"))
    if failed:
        # Fail-stop. 2026-09-16: it used to say this EVERY 15 MINUTES, and on 09-14 it did so for
        # 20 hours straight. A parked queue is not silent and never was — it was noise that blended
        # in, which is the same failure as silence and harder to see. Once, then hourly.
        nag = STATE / ".park-notice"
        last = 0.0
        try:
            last = float(json.loads(nag.read_text()).get("at", 0))
        except Exception:  # noqa: BLE001
            pass
        if time.time() - last >= 3600:
            nag.write_text(json.dumps({"at": time.time(), "failed": failed}))
            print(f"apply-queue is PARKED: {len(failed)} item(s) failed and were never cleared — "
                  f"{', '.join(failed)}. Nothing else will run until the .failed marker(s) in "
                  f"{STATE} are removed. Detail: {LOG}")
        return 0
    # Queue healthy: forget the nag clock so the next park says so immediately.
    try:
        (STATE / ".park-notice").unlink()
    except FileNotFoundError:
        pass

    pending = [d for d in items
               if not d.get("_broken")
               and d.get("armed") is True
               and not (STATE / f"{d['id']}.done").exists()]
    broken = [d for d in items if d.get("_broken")]
    for d in broken:
        print(f"apply-queue: {d['id']} — {d['_broken']}")
    if not pending:
        return 0

    # The window. Checked AFTER pending is known, so an empty queue is silent at every hour of the
    # day rather than announcing that it is waiting for a window it has nothing to use.
    if not in_window() and not any(d.get("urgent") for d in pending):
        log(f"deferred to the night window ({WINDOW_OPEN_H:02d}:{WINDOW_OPEN_M:02d}–"
            f"{WINDOW_CLOSE_H:02d}:{WINDOW_CLOSE_M:02d} local): {len(pending)} item(s) waiting — "
            f"{', '.join(d['id'] for d in pending)}")
        return 0
    pending = [d for d in pending if in_window() or d.get("urgent")]

    # A kernel item that is approved on paper but not cleared (kernel_item_cleared) is WAITING, not
    # failing: it is stepped over so the rest of the queue is not parked behind it, it never lands,
    # and it says why once a day rather than every tick.
    ready = []
    for d in pending:
        sp = QUEUE / (d.get("script") or f"{d['id']}.sh")
        if sp.is_file() and d.get("core_patch_approved") and touches_kernel(sp.read_text()):
            ok, why_not = kernel_item_cleared(d)
            if not ok:
                log(f"{d['id']}: held — kernel item not cleared: {why_not}")
                note = STATE / f".kernel-wait-{d['id']}"
                last = 0.0
                try:
                    last = float(json.loads(note.read_text()).get("at", 0))
                except Exception:  # noqa: BLE001
                    pass
                if time.time() - last >= 86400:
                    note.write_text(json.dumps({"at": time.time(), "why": why_not}))
                    print(f"apply-queue: HELD {d['id']} — it patches the kernel and is not cleared to "
                          f"land: {why_not}. Nothing was run; later items are not blocked by it.")
                continue
        ready.append(d)
    if not ready:
        return 0
    pending = ready

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
        # ROLL BACK, do not merely recommend it. Unattended means nobody reads the recommendation
        # until morning, and a half-applied change sitting live for six hours is the risk the night
        # window creates. Failing to roll back is itself recorded and reported loudly.
        rb_ok, rb_msg = rollback(item.get("manifest"))
        rec["rollback"] = {"attempted": bool(item.get("manifest")), "ok": rb_ok, "detail": rb_msg}
        (STATE / f"{iid}.failed").write_text(json.dumps(rec, indent=1))
        if item.get("manifest") and rb_ok:
            state_line = (f"ROLLED BACK automatically from manifest {item['manifest']} — the fleet "
                          f"is as it was before this item ran.")
        elif item.get("manifest"):
            state_line = (f"⚠️ ROLLBACK FAILED for manifest {item['manifest']} — the change may be "
                          f"HALF APPLIED. Run `fleet-rollback.sh {item['manifest']}` and read it. "
                          f"Detail: {rb_msg}")
        else:
            state_line = ("⚠️ No manifest was declared, so nothing could be rolled back "
                          "automatically. Check what the item changed before re-arming it.")
        (STATE / f"{iid}.failed").write_text(json.dumps(rec, indent=1))
        print(f"apply-queue: FAILED {iid} — {item.get('title', '')} (rc={rc} after {took:.0f}s).\n"
              f"  {state_line}\n"
              f"  The queue is now PARKED; nothing further runs until "
              f"{STATE / (iid + '.failed')} is removed.\n{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
