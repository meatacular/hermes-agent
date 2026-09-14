#!/usr/bin/env python3
"""Test harness for worker-stall-watch.py.

Proves the watch goes RED on a real idle-but-running worker and stays SILENT on a worker that
is quiet but burning CPU (the false positive the 2026-09-14 card calls out). Runs against a
throwaway HERMES_HOME so the live board is never touched.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "worker-stall-watch.py"

SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
  worker_pid INTEGER, last_heartbeat_at INTEGER, started_at INTEGER,
  model_override TEXT, current_run_id INTEGER
);
CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, started_at INTEGER
);
"""


def build_home(tmp: Path):
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "kanban" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp / "fleet").mkdir(parents=True, exist_ok=True)
    (tmp / "config.yaml").write_text(
        "model:\n  provider: modelark\n  default: deepseek-v4-flash-ga-260731\n")
    # production shape: each profile has its own compiled config, and they DIFFER — the alert
    # must report the assignee's model, not root's.
    for prof, mid, prov in (("steve-o", "deepseek-v4-flash-ga-260731", "modelark"),
                            ("karl", "deepseek-v4-flash-ga-260731", "modelark"),
                            ("bob", "openai/gpt-5.6-luna", "openrouter")):
        d = tmp / "profiles" / prof
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(f"model:\n  provider: {prov}\n  default: {mid}\n")
    (tmp / "fleet" / "models.yaml").write_text(
        "models:\n  ma-v4-flash:\n    id: deepseek-v4-flash-ga-260731\n    short: v4-flash @ModelArk\n")
    con = sqlite3.connect(tmp / "kanban.db")
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def add_card(tmp: Path, tid, assignee, pid, silent_min, run_min,
             model_override=None, run_id=1, heartbeat_age_s=30):
    now = time.time()
    con = sqlite3.connect(tmp / "kanban.db")
    con.execute(
        "INSERT OR REPLACE INTO tasks (id,title,assignee,status,worker_pid,last_heartbeat_at,"
        "started_at,model_override,current_run_id) VALUES (?,?,?,'running',?,?,?,?,?)",
        (tid, f"Test card {tid}", assignee, pid,
         int(now - heartbeat_age_s), int(now - run_min * 60), model_override, run_id))
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id,task_id,status,started_at) VALUES (?,?,'running',?)",
        (run_id, tid, int(now - run_min * 60)))
    con.commit()
    con.close()
    log = tmp / "kanban" / "logs" / f"{tid}.log"
    log.write_text("worker stdout\n")
    old = now - silent_min * 60
    os.utime(log, (old, old))


def clear_boards(tmp: Path):
    con = sqlite3.connect(tmp / "kanban.db")
    con.execute("DELETE FROM tasks")
    con.execute("DELETE FROM task_runs")
    con.commit()
    con.close()
    for f in (tmp / "kanban" / "logs").glob("*.log"):
        f.unlink()
    st = tmp / "state" / "worker-stall-watch.json"
    if st.exists():
        st.unlink()


def run(tmp: Path, args=()):
    env = dict(os.environ, HERMES_HOME=str(tmp))
    p = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                       env=env, timeout=120)
    return p.stdout.strip(), p.stderr.strip(), p.returncode


def alive(p):
    return p.poll() is None


def spawn_idle():
    """A process that is alive and blocked — the shape of a worker stuck on a dead socket."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])


def spawn_busy():
    """A process alive and burning CPU — the shape of a long silent test/compile."""
    return subprocess.Popen([sys.executable, "-c",
                             "import time\n"
                             "t=time.time()\n"
                             "while time.time()-t < 600:\n"
                             "    x=0\n"
                             "    for i in range(200000): x+=i\n"])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wsw-test-"))
    results, failures = [], []
    idle = busy = None
    try:
        build_home(tmp)
        time.sleep(1.0)

        def check(name, cond, detail=""):
            results.append((name, bool(cond), detail))
            if not cond:
                failures.append(name)

        # --- T1: no running cards -> silent -------------------------------------
        clear_boards(tmp)
        out, err, rc = run(tmp)
        check("T1 silent when no running cards", out == "" and rc == 0, repr(out[:120]))

        # --- T2: quiet + genuinely idle PID -> RED (AC1, AC2) -------------------
        idle = spawn_idle()
        time.sleep(1.0)
        clear_boards(tmp)
        add_card(tmp, "t_idle0001", "steve-o", idle.pid, silent_min=6, run_min=9)
        out, err, rc = run(tmp)
        check("T2 idle worker reported", "t_idle0001" in out, out[:200])
        check("T2 AC2 assignee on alert", "steve-o" in out)
        check("T2 AC2 pid on alert", str(idle.pid) in out)
        check("T2 AC2 elapsed on alert", "run 9 min" in out)
        check("T2 AC2 model on alert", "v4-flash @ModelArk" in out, out[:300])
        check("T2 CPU shown idle", "CPU idle" in out)
        check("T2 tier is WAITING at 6 min", "WAITING" in out)

        # --- T2b: the assignee's OWN model is reported, not root's --------------
        clear_boards(tmp)
        add_card(tmp, "t_bobmodel", "bob", idle.pid, silent_min=6, run_min=9, run_id=11)
        out, err, rc = run(tmp)
        check("T2b assignee's own model reported",
              "openai/gpt-5.6-luna" in out and "modelark" not in out.split("model:")[1][:80],
              out[:300])

        # --- T3: quiet but BURNING CPU -> suppressed (no false positive) --------
        busy = spawn_busy()
        time.sleep(2.0)
        clear_boards(tmp)
        add_card(tmp, "t_busy0001", "bob", busy.pid, silent_min=6, run_min=9)
        out, err, rc = run(tmp)
        check("T3 busy worker suppressed", out == "" and rc == 0, repr(out[:200]))
        st = json.loads((tmp / "state" / "worker-stall-watch.json").read_text())
        check("T3 busy recorded as WORKING",
              any("WORKING" in s for s in st.get("suppressed_working", [])),
              str(st.get("suppressed_working")))

        # --- T4: idle + past stale floor -> STALLED ----------------------------
        clear_boards(tmp)
        add_card(tmp, "t_stalled01", "rodge", idle.pid, silent_min=12, run_min=20)
        out, err, rc = run(tmp)
        check("T4 STALLED past stale floor", "STALLED" in out and "t_stalled01" in out, out[:200])

        # --- T5: busy but past the stale floor -> still reported ---------------
        clear_boards(tmp)
        add_card(tmp, "t_busylong", "karl", busy.pid, silent_min=12, run_min=20)
        out, err, rc = run(tmp)
        check("T5 busy past stale floor still reported",
              "t_busylong" in out and "STALLED" in out, out[:200])

        # --- T6: quiet below the 5-min threshold -> silent ---------------------
        clear_boards(tmp)
        add_card(tmp, "t_fresh001", "jobsy", idle.pid, silent_min=2, run_min=3)
        out, err, rc = run(tmp)
        check("T6 silent below 300s threshold", out == "", repr(out[:120]))

        # --- T7: per-card model pin wins ---------------------------------------
        clear_boards(tmp)
        add_card(tmp, "t_pinned01", "bob", idle.pid, silent_min=6, run_min=9,
                 model_override="luna", run_id=7)
        out, err, rc = run(tmp)
        check("T7 per-card model pin shown", "luna" in out and "per-card pin" in out, out[:300])

        # --- T8: exits 0 and silent on a healthy board -------------------------
        clear_boards(tmp)
        out, err, rc = run(tmp)
        check("T8 healthy board silent + exit 0", out == "" and rc == 0, repr(out[:120]))

        # --- T9..T12: the --auto-kill path (opt-in, NOT wired into cron) -------
        # T9: STALLED + idle CPU -> the worker is actually signalled
        k1 = spawn_idle()
        time.sleep(1.0)
        clear_boards(tmp)
        add_card(tmp, "t_killstall", "bob", k1.pid, silent_min=12, run_min=20, run_id=21)
        out, err, rc = run(tmp, ("--auto-kill",))
        time.sleep(1.0)
        check("T9 stalled idle worker killed", not alive(k1), out[-260:])
        check("T9 kill reported on the alert", "auto-kill: SIGTERM sent" in out, out[-260:])

        # T10: WAITING (6 min, below the stale floor) -> NEVER killed
        k2 = spawn_idle()
        time.sleep(1.0)
        clear_boards(tmp)
        add_card(tmp, "t_killwait", "bob", k2.pid, silent_min=6, run_min=9, run_id=22)
        out, err, rc = run(tmp, ("--auto-kill",))
        check("T10 WAITING worker NOT killed", alive(k2), out[:200])
        check("T10 WAITING reported", "WAITING" in out and "t_killwait" in out, out[:200])
        check("T10 no kill line on WAITING", "auto-kill:" not in out, out[:250])

        # T11: past the floor but burning CPU -> NEVER killed
        k3 = spawn_busy()
        time.sleep(2.0)
        clear_boards(tmp)
        add_card(tmp, "t_killbusy", "bob", k3.pid, silent_min=12, run_min=20, run_id=23)
        out, err, rc = run(tmp, ("--auto-kill",))
        check("T11 CPU-busy worker NOT killed", alive(k3), out[:250])
        check("T11 busy worker still reported", "STALLED" in out and "t_killbusy" in out, out[:250])
        check("T11 no kill line on busy worker", "auto-kill:" not in out, out[:300])

        # T12: plain run must never kill anything, even a real stall
        k4 = spawn_idle()
        time.sleep(1.0)
        clear_boards(tmp)
        add_card(tmp, "t_nokill01", "bob", k4.pid, silent_min=12, run_min=20, run_id=24)
        out, err, rc = run(tmp)
        check("T12 default run does NOT kill", alive(k4), out[:250])
        check("T12 default run still reports", "t_nokill01" in out, out[:250])

    finally:
        for p in (idle, busy, locals().get("k1"), locals().get("k2"),
                  locals().get("k3"), locals().get("k4")):
            if p:
                try:
                    p.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}  {detail if not ok else ''}")
    print(f"\n{len(results) - len(failures)}/{len(results)} passed")
    if failures:
        print("FAILURES: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
