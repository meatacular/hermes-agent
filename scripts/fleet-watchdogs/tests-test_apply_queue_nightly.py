#!/usr/bin/env python3
"""Controls for the 2026-09-16 nightly-window + auto-rollback changes to apply-queue.

Three things must be true, and each is a way the unattended window could hurt:
  1. the window opens and closes at the right local times, INCLUDING across midnight;
  2. an `urgent` item bypasses it, because a fix that must not wait a day needs a door;
  3. a failed item is ROLLED BACK, not merely told to roll itself back.
"""
import importlib.util, json, os, sys, tempfile, time
from pathlib import Path

os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
SRC = Path(os.environ["HERMES_HOME"]) / "hermes-agent/scripts/fleet-watchdogs/apply-queue.py"
spec = importlib.util.spec_from_file_location("aq", SRC)
aq = importlib.util.module_from_spec(spec); spec.loader.exec_module(aq)

F = []
def check(name, got, want):
    ok = got == want; F.append(name) if not ok else None
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")

def at(h, m):
    t = list(time.localtime()); t[3], t[4] = h, m
    return time.mktime(time.struct_time(tuple(t)))

print("\n1. the window (22:30 -> 05:30 local, wrapping midnight)")
for h, m, want in [(22, 29, False), (22, 30, True), (23, 59, True), (0, 1, True),
                   (5, 29, True), (5, 30, False), (12, 0, False), (17, 0, False)]:
    check(f"{h:02d}:{m:02d}", aq.in_window(at(h, m)), want)

print("\n2. rollback is CALLED, and reports honestly")
check("no manifest -> refuses, does not claim success", aq.rollback("")[0], False)
check("no manifest -> says why", "nothing to roll back" in aq.rollback("")[1], True)
ok, msg = aq.rollback("definitely-not-a-real-manifest-20260916")
check("unknown manifest -> not ok", ok, False)
print(f"       (detail: {msg.splitlines()[0][:90] if msg else '—'})")

print("\n3. mutation controls — each guard must be load-bearing")
src = SRC.read_text()
assert "def in_window" in src and "def rollback" in src, "the functions are gone"
# the window must actually gate: remove the gate and pending items would run at any hour
assert "if not in_window() and not any(d.get(\"urgent\") for d in pending)" in src, \
    "the window gate is not wired into main() — in_window() would be decoration"
# the failure branch must CALL rollback, not print a suggestion
fail_branch = src.split("    else:\n        # ROLL BACK")[1][:1200]
assert "rb_ok, rb_msg = rollback(" in fail_branch, "the failure branch does not call rollback()"
assert "rec[\"rollback\"]" in fail_branch, "the rollback outcome is not recorded in .failed"
print("  ok   in_window() is wired into main(), not just defined")
print("  ok   the failure branch calls rollback() and records its outcome")

print(f"\n{'FAILED: ' + ', '.join(F) if F else 'ALL PASS'} — {len(F)} failure(s)")
sys.exit(1 if F else 0)
