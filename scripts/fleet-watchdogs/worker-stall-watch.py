#!/usr/bin/env python3
"""worker-stall-watch — a running card whose worker has gone SILENT.

``kanban-liveness-watch`` sees a dead dispatcher. ``stalled-card-watch`` sees a cold worker
heartbeat. Neither sees the case measured on 2026-09-13: the heartbeat was FRESH every 60s
for ten minutes while the worker's main thread was blocked on a single model API call, because
**the heartbeat is written by a different thread from the one doing the work**. From the board
the card read `running` and healthy. Diagnosis was only possible at the OS level:

    ps -o pid,etime,%cpu,cputime,stat -p <PID>   # 1.3% CPU, 24s CPU in 15 min wall
    sample <PID> 1                               # main thread in __psynch_cvwait
    lsof -p <PID> | grep ESTABLISHED             # connection open, no response body

The per-card worker log (``kanban/logs/<id>.log``) is the honest progress signal: it is the
worker's own stdout, so its mtime is the last time the worker did anything at all.

CPU CORROBORATION (added 2026-09-14)
------------------------------------
Log silence alone cannot tell a blocked socket from a long CPU-bound job — a 10-minute
``pytest`` run writes no stdout and looks identical to a dead API call. The CPU reading is
added to separate those two, and it is measured as a **cputime delta over a wall window**,
not as raw ``ps -o %cpu=``. macOS ``%cpu`` is a decaying average: measured here, a process
burning a core for 20s and then idling reported 99.0% mid-burn but 0.0% within three seconds
of idling, so as a single reading it is ambiguous. The delta is exact.

What CPU does NOT do is separate a dead socket from a healthy-but-slow reasoning call: both
sit at ~0% CPU. That distinction is the reasoning stale floor's job, not this watch's, which
is why the ALERT tier below is still pegged to it. **CPU suppresses false positives; it is not
a stall oracle.**

**Two thresholds, and the second one is the point.** Hermes deliberately waits a long time for
a reasoning model: ``agent/reasoning_timeouts.py`` floors the stale-stream detector at 600s for
the deepseek v4-flash/v4-pro and R1 families (they stream ``reasoning_content`` for minutes
before any content). So:

  * WAITING (>= WARN_MIN): the worker is quiet and NOT burning CPU. Usually a legitimate long
    think. Reported so a human knows, but the text says explicitly not to kill it.
  * STALLED (>= ALERT_MIN): past the LONGEST stale budget the fleet grants any model. The
    stale-stream detector should already have aborted this call. That it has not is a fault.

A quiet worker that IS burning CPU (>= CPU_BUSY_PCT) is reported as WORKING and never as a
stall — that is a long test or a compile, not a dead socket.

Silent unless something is wrong, zero tokens, stdlib only (cron runs the SYSTEM python3).

``--auto-kill`` (alias ``--kill``) implements the card's item 5 but is **NOT wired into cron**.
Default behaviour, and the only behaviour the fleet runs, is detection-only: ``cron`` invokes the
script with no arguments. Killing a worker is destructive and the fleet's standing rule is that
destructive actions are explicit and human-confirmed, never silent — so the flag has to be passed
by hand. It is additionally gated: it will only ever signal a worker already classified STALLED
(past the stale floor AND idle CPU). A WAITING worker, or one burning CPU, is never killed.
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
DB = HERMES_HOME / "kanban.db"
WORKER_LOGS = HERMES_HOME / "kanban" / "logs"
STATE = HERMES_HOME / "state" / "worker-stall-watch.json"
MODELS_YAML = HERMES_HOME / "fleet" / "models.yaml"

# 300s: the 2026-09-14 card's threshold, matched to the WP2 observation that stalls exceeded
# 5 min while normal model calls complete inside 2. A quiet worker at 5 min is REPORTED, not
# killed — the message says so, because on 09-13 a healthy worker was killed nine seconds
# before the 600s detector would have recovered it by itself.
WARN_MIN = float(os.environ.get("STALL_WARN_MIN", "5"))
# 600s is the largest reasoning stale floor in agent/reasoning_timeouts.py; +1 min of slack so a
# detector that is about to fire correctly is not reported as a fault.
ALERT_MIN = float(os.environ.get("STALL_ALERT_MIN", "11"))
# Instantaneous CPU% at or above which the worker is considered to be making real progress.
CPU_BUSY_PCT = float(os.environ.get("STALL_CPU_BUSY_PCT", "5"))
# Wall window over which the cputime delta is measured. Short: this only runs for already-silent
# cards, which is the rare case.
CPU_WINDOW_S = float(os.environ.get("STALL_CPU_WINDOW_S", "2.0"))

# profile name -> models.yaml agent key. The compiled config.yaml is the first source tried, so
# this is only the fallback label lookup.
_PROFILE_ALIASES = {"default": "root", "smith": "root", "agent-smith": "root"}


def _cputime_seconds(pid):
    """Total CPU seconds (user+sys) consumed by pid, or None if it cannot be read."""
    try:
        out = subprocess.run(
            ["ps", "-o", "cputime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — a watchdog never dies on a ps quirk
        return None
    if not out:
        return None
    days = 0
    if "-" in out:                      # BSD ps: dd-hh:mm:ss
        head, out = out.split("-", 1)
        try:
            days = int(head)
        except ValueError:
            return None
    total = 0.0
    for part in out.split(":"):         # [[hh:]mm:]ss[.cc]
        try:
            total = total * 60.0 + float(part)
        except ValueError:
            return None
    return days * 86400.0 + total


def _cpu_pct(pid):
    """Instantaneous CPU% over CPU_WINDOW_S, as (pct, detail). None pct = unmeasurable."""
    if not pid:
        return None, "no pid recorded"
    first = _cputime_seconds(pid)
    if first is None:
        return None, "process not readable"
    t0 = time.time()
    time.sleep(CPU_WINDOW_S)
    second = _cputime_seconds(pid)
    if second is None:
        return None, "process exited during sample"
    wall = max(time.time() - t0, 0.001)
    delta = max(second - first, 0.0)
    return delta / wall * 100.0, f"{delta:.2f}s CPU in {wall:.1f}s wall"


def _model_label(profile, override):
    """Human-facing model for a card: the pin if any, else the assignee's compiled config."""
    if override:
        return f"{override} (per-card pin)"
    prof = (profile or "default").strip() or "default"
    if prof in ("default", "root"):
        cand = [HERMES_HOME / "config.yaml"]
    else:
        # profile config first, root config as the fallback — a profile with no compiled config
        # must still produce a model line rather than "unknown".
        cand = [HERMES_HOME / "profiles" / prof / "config.yaml", HERMES_HOME / "config.yaml"]
    model_id = provider = None
    for path in cand:
        try:
            import yaml

            cfg = yaml.safe_load(path.read_text()) or {}
            blk = cfg.get("model") or {}
            if isinstance(blk, dict) and blk.get("default"):
                model_id = blk["default"]
                provider = blk.get("provider")
                break
        except Exception:  # noqa: BLE001
            continue
    if not model_id:
        return "unknown (config unreadable)"
    try:  # nicer label when models.yaml knows the id
        import yaml

        spec = yaml.safe_load(MODELS_YAML.read_text()) or {}
        for entry in (spec.get("models") or {}).values():
            if entry.get("id") == model_id and entry.get("short"):
                return f"{entry['short']}" + (f" via {provider}" if provider else "")
    except Exception:  # noqa: BLE001
        pass
    return model_id + (f" via {provider}" if provider else "")


def _kill(pid):
    """SIGTERM a stalled worker. Returns (ok, detail). Never raises."""
    try:
        os.kill(int(pid), 15)
        return True, "SIGTERM sent; the dispatcher reclaims the run and retries"
    except ProcessLookupError:
        return False, "process already gone"
    except Exception as e:  # noqa: BLE001
        return False, f"kill failed: {e}"


def main() -> int:
    auto_kill = any(a in ("--auto-kill", "--kill") for a in sys.argv[1:])
    if not DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, title, assignee, worker_pid, last_heartbeat_at, started_at, "
            "model_override, current_run_id FROM tasks WHERE status = 'running'").fetchall()
        # run start times, for a true elapsed-time figure rather than only the silence window
        run_start = {}
        for r in con.execute(
                "SELECT id, started_at FROM task_runs WHERE status = 'running'"):
            run_start[r[0]] = r[1]
    except sqlite3.Error as e:
        print(f"worker-stall-watch: cannot read kanban.db: {e}", file=sys.stderr)
        return 0

    now = time.time()
    findings, fingerprint, suppressed = [], [], []
    for t in rows:
        log = WORKER_LOGS / f"{t['id']}.log"
        try:
            silent = (now - log.stat().st_mtime) / 60.0
        except OSError:
            # No log yet. Bounded: a spawn that has written nothing after WARN_MIN is
            # itself the fault — 2026-09-14: a worker hung 91 min having never written
            # a byte, and this branch skipped it on all 50 watchdog runs (t_8db0bc24).
            rs = run_start.get(t["current_run_id"]) or t["started_at"]
            age = (now - float(rs)) / 60.0 if rs else None
            if age is None or age < WARN_MIN:
                continue          # genuinely still coming up
            silent, no_log = age, True
        else:
            no_log = False
        if silent < WARN_MIN:
            continue

        pid = t["worker_pid"]
        cpu_pct, cpu_detail = _cpu_pct(pid)
        busy = cpu_pct is not None and cpu_pct >= CPU_BUSY_PCT
        if busy and silent < ALERT_MIN:
            # Real progress: a long test or a compile writes no stdout but burns CPU. Reporting
            # this would be the false positive the card explicitly calls out.
            suppressed.append(f"{t['id']}:WORKING(cpu {cpu_pct:.1f}%)")
            continue

        elapsed = None
        rs = run_start.get(t["current_run_id"]) or t["started_at"]
        if rs:
            elapsed = (now - float(rs)) / 60.0

        level = "STALLED" if silent >= ALERT_MIN else "WAITING"
        hb = t["last_heartbeat_at"]
        hb_age = (now - float(hb)) / 60.0 if hb else None
        # A fresh heartbeat next to a silent worker is the whole signal — say so explicitly,
        # because "running + fresh heartbeat" is exactly what makes this invisible.
        hb_txt = ("heartbeat FRESH %.0f min" % hb_age) if hb_age is not None and hb_age < 3 \
            else ("heartbeat %.0f min old" % hb_age if hb_age is not None else "no heartbeat")
        if busy:
            cpu_txt = f"CPU {cpu_pct:.1f}% ({cpu_detail}) — burning CPU past the stale floor"
        elif cpu_pct is None:
            cpu_txt = f"CPU unmeasurable ({cpu_detail})"
        else:
            cpu_txt = f"CPU idle {cpu_pct:.1f}% ({cpu_detail})"
        elapsed_txt = f"run {elapsed:.0f} min" if elapsed is not None else "run unknown"
        kill_txt = ""
        if auto_kill and level == "STALLED" and not busy:
            # Gated: only a worker past the stale floor whose CPU is idle. A WAITING worker or a
            # CPU-busy one is never killed, because those are the false-positive shapes.
            ok, detail = _kill(pid)
            kill_txt = f"\n          auto-kill: {detail}" + ("" if ok else " (no action)")
            log_cmd = "(no log file)" if no_log else f"tail -3 {log}"
            findings.append(
                f"  {level:<8}{t['id']} [{t['assignee'] or '-'}] no worker output for {silent:.0f} min "
                f"({elapsed_txt}, {hb_txt})\n"
                f"          model: {_model_label(t['assignee'], t['model_override'])}\n"
                f"          pid {pid or '?'} — {cpu_txt}{kill_txt}\n"
                f"          {(t['title'] or '')[:78]}\n"
                f"          " + (
                "past the 600s reasoning stale floor — the stream detector should have aborted "
                "this call and has not. Treat as a fault."
                if level == "STALLED" else
                "probably a legitimate long think (reasoning models stream for minutes; the stale "
                "detector recovers at 600s). Do NOT kill it yet.") + "\n"
                f"          ps -o pid,etime,%cpu,cputime,stat -p {pid or '<PID>'}   "
                f"| sample {pid or '<PID>'} 1   | {log_cmd}")
        fingerprint.append(f"{t['id']}:{level}")

    con.close()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    prev = []
    try:
        prev = (json.loads(STATE.read_text()) or {}).get("fingerprint") or []
    except (OSError, ValueError):
        pass
    STATE.write_text(json.dumps(
        {"at": int(now), "fingerprint": fingerprint, "suppressed_working": suppressed}))

    if not findings or sorted(fingerprint) == sorted(prev):
        return 0
    print("\n".join([f"{len(findings)} running card(s) with a silent worker "
                     f"(the heartbeat thread is NOT the working thread):"] + findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
