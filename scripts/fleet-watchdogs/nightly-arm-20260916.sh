#!/bin/bash
# 22:30 local — open tonight's deploy window by ARMING what Richie approved today.
# Arming is the approval act in this queue's design, so it is the only thing that has to happen on
# a schedule; the drainer's own 15-minute tick does the rest, one item at a time, and rolls back
# anything that fails. Idempotent: arming an already-armed item is a no-op.
set -uo pipefail
H="$HOME/.hermes"; LOG="$H/logs/nightly-arm.log"
exec >> "$LOG" 2>&1
echo "=== $(date '+%F %T %Z') nightly-arm ==="
python3 - <<'PYX'
import json, pathlib
Q = pathlib.Path.home()/".hermes"/"scripts"/"apply-queue"
ST = pathlib.Path.home()/".hermes"/"state"/"apply-queue"
armed, skipped = [], []
for p in sorted(Q.glob("*.json")):
    try:
        d = json.loads(p.read_text())
    except Exception as exc:
        print(f"  ! {p.name} unreadable: {exc}"); continue
    if (ST / f"{d.get('id', p.stem)}.done").exists():
        continue                                  # already landed
    if d.get("armed"):
        continue                                  # already armed
    # A descriptor that explains WHY it is disarmed is a decision, not an oversight. Respect it.
    if d.get("reason") or d.get("why_disarmed") or d.get("disarmed_why"):
        if d.get("arm_at"):                       # ...unless it was disarmed only to wait for tonight
            d["armed"] = True
            p.write_text(json.dumps(d, indent=2) + "\n")
            armed.append(d.get("id", p.stem))
        else:
            skipped.append((d.get("id", p.stem),
                            (d.get("reason") or d.get("why_disarmed") or d.get("disarmed_why"))[:80]))
        continue
    d["armed"] = True
    p.write_text(json.dumps(d, indent=2) + "\n")
    armed.append(d.get("id", p.stem))
print(f"  armed: {armed or 'nothing new'}")
for i, why in skipped:
    print(f"  left disarmed (deliberate): {i} — {why}")
PYX
echo "  the drainer takes it from here; window 22:30-05:30, one item per tick, rollback on failure"
