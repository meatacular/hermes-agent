#!/usr/bin/env python3
"""END-TO-END proof that a failed queue item is actually rolled back (2026-09-16).

The unit controls prove `rollback()` is called. This proves the whole chain does what it claims:
a real manifest with a real pre-image, a real queue item that CHANGES A FILE and then fails, the
real drainer — and the file must come back byte-identical.

It runs entirely inside a temporary HERMES_HOME. `change-manifest.py` and `apply-queue.py` both
honour that variable, so nothing here can reach the live fleet.

The negative control is the point: with the rollback call removed, the canary must stay corrupted.
Without that, this suite would pass just as happily against a drainer that rolls back nothing.
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

LIVE = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
DRAINER = LIVE / "hermes-agent/scripts/fleet-watchdogs/apply-queue.py"
MANIFEST = LIVE / "scripts/change-manifest.py"
F = []

def check(name, got, want):
    ok = got == want
    if not ok: F.append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")

def build(sandbox: Path, drainer_src: str) -> Path:
    """One disposable HERMES_HOME with a canary, a manifest holding its pre-image, and one
    queue item that corrupts the canary and then fails."""
    for d in ("scripts/apply-queue", "state/apply-queue", "changes", "logs", "backups"):
        (sandbox / d).mkdir(parents=True, exist_ok=True)
    canary = sandbox / "state" / "canary.txt"
    canary.write_text("ORIGINAL\n")
    shutil.copy2(MANIFEST, sandbox / "scripts" / "change-manifest.py")
    drainer = sandbox / "scripts" / "apply-queue-under-test.py"
    drainer.write_text(drainer_src)

    env = {**os.environ, "HERMES_HOME": str(sandbox)}
    subprocess.run([sys.executable, str(sandbox / "scripts/change-manifest.py"), "new", "rbtest",
                    "--title", "rollback e2e canary", "--restart", "none"],
                   env=env, capture_output=True, text=True, timeout=60)
    r = subprocess.run([sys.executable, str(sandbox / "scripts/change-manifest.py"), "file",
                        "rbtest", str(canary)], env=env, capture_output=True, text=True, timeout=60)
    assert (sandbox / "changes" / "rbtest.json").is_file(), r.stdout + r.stderr

    (sandbox / "scripts/apply-queue/001-rbtest.json").write_text(json.dumps({
        "id": "001-rbtest", "title": "corrupt the canary, then fail", "armed": True,
        "script": "001-rbtest.sh", "requires_idle_board": False, "requires_clean_tree": False,
        "urgent": True, "restart": [], "manifest": "rbtest"}))
    sh = sandbox / "scripts/apply-queue/001-rbtest.sh"
    sh.write_text(f'#!/bin/bash\nprintf "CORRUPTED\\n" > "{canary}"\necho "canary corrupted on purpose"\nexit 1\n')
    sh.chmod(0o755)
    return canary

def run(sandbox: Path):
    env = {**os.environ, "HERMES_HOME": str(sandbox)}
    return subprocess.run([sys.executable, str(sandbox / "scripts/apply-queue-under-test.py")],
                          env=env, capture_output=True, text=True, timeout=300)

live_src = DRAINER.read_text()

print("\n1. a failing item is ROLLED BACK, unattended")
with tempfile.TemporaryDirectory(prefix="rb-e2e-") as t:
    sb = Path(t); canary = build(sb, live_src)
    r = run(sb)
    print("     drainer said:", " | ".join(l.strip() for l in r.stdout.strip().splitlines()[:3]))
    check("canary restored byte-for-byte", canary.read_text(), "ORIGINAL\n")
    check("item recorded as failed", (sb / "state/apply-queue/001-rbtest.failed").is_file(), True)
    rec = json.loads((sb / "state/apply-queue/001-rbtest.failed").read_text())
    check("rollback attempted", rec.get("rollback", {}).get("attempted"), True)
    check("rollback succeeded", rec.get("rollback", {}).get("ok"), True)
    check("operator is told it rolled back", "ROLLED BACK automatically" in r.stdout, True)
    check("and that the queue is parked", "PARKED" in r.stdout, True)

print("\n2. NEGATIVE CONTROL — with the rollback call removed, the canary must stay corrupted")
neutered = live_src.replace("rb_ok, rb_msg = rollback(item.get(\"manifest\"))",
                            "rb_ok, rb_msg = (False, 'control: rollback removed')")
assert neutered != live_src, "the control did not change the source — it proves nothing"
with tempfile.TemporaryDirectory(prefix="rb-ctl-") as t:
    sb = Path(t); canary = build(sb, neutered)
    run(sb)
    got = canary.read_text()
    check("canary stays corrupted without the rollback", got, "CORRUPTED\n")
    print("       (so case 1 is measuring the rollback, not a script that never wrote anything)")

print(f"\n{'FAILED: ' + ', '.join(F) if F else 'ALL PASS'} — {len(F)} failure(s)")
sys.exit(1 if F else 0)
