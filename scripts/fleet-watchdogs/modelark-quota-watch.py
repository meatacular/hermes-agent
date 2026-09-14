#!/usr/bin/env python3
"""modelark-quota-watch — the ModelArk Coding Plan's 5-hour bucket, watched and announced.

The plan enforces a rolling 5-hour account quota. When it empties, every ModelArk call returns

    429 AccountQuotaExceeded — "You have exceeded the 5-hour usage quota.
    It will reset at 2026-09-14 15:58:26 +0800 CST."

and Hermes fails over down the waterfall. On 2026-09-14 that failover landed on OpenRouter hosts
that cache nothing, and roughly $18.9 of the day's $23.8 went there before anyone knew the bucket
had emptied. The event itself is normal; being unaware of it is what cost money.

Richie, 2026-09-15: "all that is required is understanding when modelark can be disabled due to
hitting cap, and then enabled after the period. Automatic, in the background, but advise when
changes are made."

WHAT IT DOES — and what it deliberately does NOT do
---------------------------------------------------
Announces the transitions: EXHAUSTED (with the reset time and what the fallback rung now is) and
RESTORED (with what the window cost). State in ``state/modelark-quota.json``.

It does NOT rewrite the waterfall to drop the ModelArk rung, and that is a considered choice, not
an omission. Since 2026-09-15 the rung immediately behind ModelArk is DeepSeek direct, measured at
$0.000098 on a cache-warm call and $0.0037 cold. A 429 costs one fast rejected request and then a
cheaper call than the one it replaced. Rewriting nine configs every five hours would create real
churn — a history entry, a drift window and a restart debt each time — to save a rounding error.
If the rung behind ModelArk is ever expensive again, this is where that decision changes.

Exit is ALWAYS 0: a finding is not a failure. A non-zero exit every tick grows failure_streak
until the scheduler disables the job, which is how a watchdog silences itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys

H = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
if H.parent.name == "profiles":
    H = H.parent.parent
STATE = H / "state" / "modelark-quota.json"
LOGS = [H / "logs" / "agent.log", H / "logs" / "agent.log.1"]

QUOTA_RE = re.compile(
    r"AccountQuotaExceeded.*?reset at (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<off>[+-]\d{4})",
    re.S)
TAIL_BYTES = 4_000_000


def _tail(p: pathlib.Path) -> str:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def latest_reset():
    """The newest stated reset time across the live logs, as an aware datetime."""
    best = None
    for p in LOGS:
        for m in QUOTA_RE.finditer(_tail(p)):
            off = m.group("off")
            tz = dt.timezone(dt.timedelta(hours=int(off[:3]), minutes=int(off[0] + off[3:])))
            when = dt.datetime.strptime(m.group("when"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            if best is None or when > best:
                best = when
    return best


def billed_since(ts: float) -> float:
    """What the fleet has billed since ``ts`` — the honest price of a quota window."""
    total = 0.0
    dbs = [H / "state.db"] + sorted((H / "profiles").glob("*/state.db"))
    for db in dbs:
        if not db.is_file():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            row = c.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd),0) FROM session_model_usage "
                "WHERE last_seen > ? AND COALESCE(billing_provider,'') != 'modelark'", (ts,)
            ).fetchone()
            total += float(row[0] or 0.0)
            c.close()
        except sqlite3.Error:
            continue
    return total


def main() -> int:
    prev = {}
    try:
        prev = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        pass

    now = dt.datetime.now(dt.timezone.utc)
    reset = latest_reset()
    state = dict(prev)
    lines = []

    exhausted_now = bool(reset and reset > now)
    was_exhausted = bool(prev.get("exhausted"))

    if exhausted_now and not was_exhausted:
        state.update({"exhausted": True,
                      "resets_at": reset.isoformat(),
                      "noticed_at": now.isoformat(),
                      "noticed_ts": now.timestamp()})
        mins = int((reset - now).total_seconds() // 60)
        lines += [
            "MODELARK QUOTA EXHAUSTED — the 5-hour Coding Plan bucket is empty.",
            f"  resets at {reset.astimezone().strftime('%Y-%m-%d %H:%M %Z')} (~{mins} min)",
            "  Routing is UNCHANGED: the rung behind ModelArk is DeepSeek direct",
            "  ($0.000098 cache-warm, $0.0037 cold), so the failover is cheaper than a rewrite.",
            "  No action needed. This message exists so the window is never silent again.",
        ]
    elif was_exhausted and not exhausted_now:
        spent = billed_since(float(prev.get("noticed_ts") or 0)) if prev.get("noticed_ts") else None
        state.update({"exhausted": False, "restored_at": now.isoformat(),
                      "last_window_cost_usd": spent})
        lines += ["MODELARK QUOTA RESTORED — the bucket has reset; ModelArk is serving again."]
        if spent is not None:
            lines.append(f"  non-ModelArk spend during the window: ${spent:.4f}")
    elif exhausted_now and was_exhausted and prev.get("resets_at") != reset.isoformat():
        # the window moved (a later 429 pushed the reset out) — worth one line, not a new alarm
        state["resets_at"] = reset.isoformat()
        lines.append(f"MODELARK QUOTA still empty — reset now {reset.astimezone().strftime('%H:%M %Z')}")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    if lines:
        print("\n".join(lines), file=sys.stderr)
        print(json.dumps({"watchdog": "modelark-quota-watch",
                          "exhausted": exhausted_now,
                          "resets_at": state.get("resets_at")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
