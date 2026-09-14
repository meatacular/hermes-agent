#!/usr/bin/env python3
"""Controls for the 2026-09-15 loop breaker in release-operator-hold-watch.

The rule: this watchdog releases a standalone hold ONCE. If the card comes back blocked, a second
release would just re-run the same failure and bill for it (t_796c3fab: released 06:14, worker
blocked 06:35, overwatch re-held 06:37 — a third of a dollar for nothing). It must fire only on a
card THIS watchdog actually released, only within the window, and never on a dry run.
"""
import importlib.util, json, sys
from datetime import datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "roh", Path(__file__).resolve().parent / "release-operator-hold-watch.py")
roh = importlib.util.module_from_spec(spec); spec.loader.exec_module(roh)

def run(ago_h, bucket="released", tid="t_x", extra=None):
    e = {"id": tid, "title": "t", "rule": "no_parents"}
    if extra: e.update(extra)
    return {"run_at": (datetime.now() - timedelta(hours=ago_h)).isoformat(), "released": [], "skipped": [],
            "announced": [], bucket: [e]}

CASES = [
    ("fires on a real release 1h ago",        [run(1)],                                   "t_x", True),
    ("fires at the window edge (23h)",        [run(23)],                                  "t_x", True),
    ("silent past the window (25h)",          [run(25)],                                  "t_x", False),
    ("silent on a DRY RUN release",           [run(1, extra={"dry_run": True})],          "t_x", False),
    ("silent when only HELD before",          [run(1, bucket="skipped")],                 "t_x", False),
    ("silent when only ANNOUNCED before",     [run(1, bucket="announced")],               "t_x", False),
    ("silent for a different card",           [run(1)],                                   "t_y", False),
    ("silent on empty history",               [],                                         "t_x", False),
    ("tolerates a corrupt run entry",         ["junk", {"run_at": "nonsense"}, run(1)],   "t_x", True),
    ("newest release wins over an old miss",  [run(30), run(2)],                          "t_x", True),
]

fails = 0
for name, hist, tid, want in CASES:
    got = bool(roh.released_before(hist, tid))
    ok = got == want
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: expected {want}, got {got}")

# Mutation control: if the dry-run guard is removed the suite MUST go red, or it proves nothing.
src = (Path(__file__).resolve().parent / "release-operator-hold-watch.py").read_text()
assert 'not entry.get("dry_run")' in src, "the dry-run guard is gone — this suite would pass vacuously"
neutered = src.replace('and not entry.get("dry_run")', "")
ns = {}
exec(compile(neutered, "neutered", "exec"), ns)
assert ns["released_before"]([run(1, extra={"dry_run": True})], "t_x"), \
    "mutation control did not change behaviour — the dry-run case is not actually being tested"
print("  ok   mutation control: removing the dry-run guard flips the dry-run case")

print(f"\n{len(CASES)} cases, {fails} failed")
sys.exit(1 if fails else 0)
