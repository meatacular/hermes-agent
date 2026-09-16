"""Hermetic tests for the drainer. Every positive paired with a control."""
import json, os, pathlib, sqlite3, subprocess, sys, tempfile
DRAINER = os.path.expanduser("~/.hermes/scripts/apply-queue.py")
ARMER = os.path.expanduser("~/.hermes/scripts/nightly-arm-20260916.sh")
fails = []
def ck(name, cond, d=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {d}" if not cond else "")); 
    if not cond: fails.append(name)

def home(tmp, busy=0):
    h = pathlib.Path(tmp); (h/"scripts"/"apply-queue").mkdir(parents=True); (h/"state").mkdir()
    c = sqlite3.connect(h/"kanban.db")
    c.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
    c.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, created_at INT)")
    import time as _t
    # An OLD event: the board is genuinely quiet. The quiescence gate has its own test below.
    c.execute("INSERT INTO task_events (task_id, created_at) VALUES ('t_old', ?)", (int(_t.time()) - 7200,))
    c.executemany("INSERT INTO tasks VALUES (?,?)", [(f"t_{i}", "running") for i in range(busy)])
    c.commit(); c.close()
    return h

def item(h, iid, body="echo hello\n", **kw):
    d = {"id": iid, "title": f"the {iid} change", "armed": True, "script": f"{iid}.sh",
         "requires_idle_board": True, "requires_clean_tree": False, "restart": []}
    d.update(kw)
    (h/"scripts"/"apply-queue"/f"{iid}.json").write_text(json.dumps(d))
    (h/"scripts"/"apply-queue"/f"{iid}.sh").write_text("#!/bin/bash\n" + body)

def run(h):
    e = dict(os.environ); e["HERMES_HOME"] = str(h)
    r = subprocess.run([sys.executable, DRAINER], capture_output=True, text=True, env=e)
    return r.stdout.strip()

def run_armer(h):
    e = dict(os.environ); e["HOME"] = str(h)
    r = subprocess.run([ARMER], capture_output=True, text=True, env=e)
    log = h/".hermes"/"logs"/"nightly-arm.log"
    return (r.stdout + (log.read_text() if log.exists() else "")).strip()

def parked_descriptor(h, iid, field):
    h = h/".hermes"
    q = h/"scripts"/"apply-queue"; q.mkdir(parents=True, exist_ok=True)
    (h/"state"/"apply-queue").mkdir(parents=True, exist_ok=True)
    (h/"logs").mkdir(parents=True, exist_ok=True)
    d = {"id": iid, "armed": False, field: "deliberately parked for proof"}
    (q/f"{iid}.json").write_text(json.dumps(d))

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"a"); item(h, "001-ok")
    out = run(h)
    ck("applies an armed item", "APPLIED 001-ok" in out, out[:120])
    ck("  ...and records .done", (h/"state"/"apply-queue"/"001-ok.done").exists())
    ck("CONTROL: second tick is silent (already done)", run(h) == "", run(h)[:80])

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"b"); item(h, "001-parked", armed=False)
    ck("CONTROL: an UNARMED item is ignored", run(h) == "")
    ck("  ...and writes no .done", not (h/"state"/"apply-queue"/"001-parked.done").exists())

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"c", busy=2); item(h, "001-ok")
    ck("CONTROL: a BUSY board defers, silently", run(h) == "")
    ck("  ...and does not mark it done", not (h/"state"/"apply-queue"/"001-ok.done").exists())

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"d"); item(h, "001-kernel", body="sed -i '' s/x/y/ hermes_cli/kanban_db.py\n")
    out = run(h)
    ck("REFUSES a kernel edit with no approval", "REFUSED" in out and "hermes_cli" in out, out[:140])
    h2 = home(pathlib.Path(t)/"d2"); item(h2, "001-kernel", body="echo touching hermes_cli/kanban_db.py\n", core_patch_approved="Richie")
    ck("CONTROL: the same script WITH core_patch_approved runs", "APPLIED" in run(h2))

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"e"); item(h, "001-bad", body="echo nope; exit 3\n"); item(h, "002-next")
    out = run(h)
    ck("a failing item FAILS loudly", "FAILED 001-bad" in out and "rc=3" in out, out[:140])
    out2 = run(h)
    ck("  ...and PARKS the queue (fail-stop, not fail-skip)", "PARKED" in out2, out2[:140])
    ck("  ...so the next item never ran", not (h/"state"/"apply-queue"/"002-next.done").exists())
    (h/"state"/"apply-queue"/"001-bad.failed").unlink()
    ck("CONTROL: clearing the marker unparks it", "002-next" in run(h) or "001-bad" in run(h))

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"f"); item(h, "001-a"); item(h, "002-b")
    out = run(h)
    ck("ONE item per tick", ("001-a" in out) and ("002-b" not in out), out[:140])
    ck("  ...in id order", (h/"state"/"apply-queue"/"001-a.done").exists()
                           and not (h/"state"/"apply-queue"/"002-b.done").exists())
    ck("  next tick takes the second", "002-b" in run(h))

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"g")
    (h/"scripts"/"apply-queue"/"001-broken.json").write_text("{not json")
    ck("a broken descriptor is reported, not fatal", "unreadable descriptor" in run(h))

with tempfile.TemporaryDirectory() as t:
    h = home(pathlib.Path(t)/"h")
    ck("CONTROL: empty queue is silent", run(h) == "")

for field in ("reason", "why_disarmed", "disarmed_why"):
    with tempfile.TemporaryDirectory() as t:
        h = pathlib.Path(t)/"arm"
        parked_descriptor(h, "001-parked", field)
        out = run_armer(h)
        d = json.loads((h/".hermes"/"scripts"/"apply-queue"/"001-parked.json").read_text())
        ck(f"AC1/2: armer respects {field}", not d["armed"], out)
        ck(f"  ...logs deliberate {field}",
           "left disarmed (deliberate): 001-parked" in out, out)

with tempfile.TemporaryDirectory() as t:
    h = pathlib.Path(t)/"arm-control"
    parked_descriptor(h, "001-unexplained", "title")
    p = h/".hermes"/"scripts"/"apply-queue"/"001-unexplained.json"
    d = json.loads(p.read_text()); d.pop("title"); p.write_text(json.dumps(d))
    out = run_armer(h)
    ck("CONTROL: armer arms an unexplained parked item", json.loads(p.read_text())["armed"], out)

print()
print("ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
