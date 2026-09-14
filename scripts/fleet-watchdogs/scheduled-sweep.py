#!/usr/bin/env python3
"""scheduled-sweep — the timer that `scheduled` never had.

Why this exists
---------------
The platform has a `scheduled` status and documents it precisely
(`kanban_db.py:8662`): *"scheduled tasks are intentionally not dispatchable; an
external cron, human action, or automation can later call unblock_task"*. That
automation was never written. So every card waiting on a **date** was parked in
`blocked` instead — and `blocked` means "something went wrong", so
`stalled-card-watch` (every 15 min, straight to Richie's DM) flagged it as a
stall every quarter hour for as long as it waited. `t_d770dc07`, the E1 7-day
verdict card, sat exactly like that from 2026-09-03: correctly waiting for
2026-09-10, indistinguishable from broken.

Two halves of the fix:
  * `scheduled` is already SILENT in every watchdog — verified 2026-09-05 against
    escalation-watch, fleet-preflight, stalled-card-watch, kanban-digest-watch
    (its NON_TERMINAL tuple deliberately omits it), job-regression-watch and
    job-complete-watch. So parking a waiting card there costs no code.
  * There is NO due-date column anywhere in the schema, and no snooze concept.
    So the date lives in a comment, in the same shape as the fleet's existing
    `cost-estimate:` / `cost-extension:` comment conventions:

        scheduled-until: 2026-09-10T17:41:00+12:00
        reason: E1 7-day verdict window opens

    On the card, so the dashboard shows it, it survives a DB restore, and there
    is exactly one source of truth.

What this does (SILENT unless it acted or something is wrong):
  * promotes every `scheduled` card whose `scheduled-until:` has passed, via the
    sanctioned CLI path (`hermes kanban unblock`) — never by direct SQL
  * on the digest weekday only, lists `scheduled` cards with NO parseable marker
    (the failure mode this pattern creates: a card that would sleep for ever)
  * writes a heartbeat so its own death is detectable — a control you have never
    seen fire is not a control you have. `fleet-preflight` asserts the freshness.

`no_agent`, zero LLM tokens, exit 0 always (a watchdog that crashes the cron is
worse than one that reports). Read-only except for the CLI promotions.

Testing flags: --db PATH, --now ISO8601, --dry-run, --digest/--no-digest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent

DEFAULT_DB = HERMES_HOME / "kanban.db"
STATE = HERMES_HOME / "state" / "scheduled-sweep.json"
HERMES_BIN = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes"

# Undated scheduled cards are reported ONCE A WEEK, never daily. Silence for
# cards that are waiting; one nudge a week for cards that are lost.
DIGEST_WEEKDAY = int(os.environ.get("DIGEST_WEEKDAY", "0"))  # 0 = Monday

MARKER = re.compile(r"^\s*scheduled[-_]until\s*:\s*(.+?)\s*$", re.I | re.M)


def parse_when(raw: str) -> datetime | None:
    """Accept ISO8601 with offset, ISO without offset (local), or a bare date.

    A bare date means 00:00 local on that day. Anything unparseable returns
    None and the card is treated as UNDATED — never as due. Fail closed on
    promotion; a card that sleeps is recoverable, a card promoted early is a
    fabricated result (which is the whole reason t_d770dc07 exists).
    """
    raw = raw.strip().strip('`"\'')
    raw = re.sub(r"\s*\(.*\)\s*$", "", raw)          # drop trailing "(NZST)" etc
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    for attempt in (raw, raw.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(attempt)
            return dt if dt.tzinfo else dt.astimezone()
        except ValueError:
            pass
    try:
        return datetime.strptime(raw, "%Y-%m-%d").astimezone()
    except ValueError:
        return None


def latest_marker(conn: sqlite3.Connection, task_id: str):
    """Newest comment carrying a marker wins — a later comment can reschedule."""
    for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY created_at DESC, id DESC",
        (task_id,),
    ):
        body = r["body"] or ""
        m = MARKER.search(body)
        if m:
            when = parse_when(m.group(1))
            if when:
                return when, m.group(1).strip()
            return None, m.group(1).strip()   # present but unparseable
    return None, None


def promote(task_id: str, when_raw: str, dry: bool) -> tuple[bool, str]:
    reason = (f"scheduled-sweep {datetime.now().astimezone().date()}: "
              f"scheduled-until {when_raw} has passed; returning to ready.")
    if dry:
        return True, "DRY-RUN (not promoted)"
    if not HERMES_BIN.exists():
        return False, f"hermes binary missing at {HERMES_BIN}"
    try:
        p = subprocess.run(
            [str(HERMES_BIN), "kanban", "unblock", "--reason", reason, task_id],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"unblock raised: {e}"
    if p.returncode != 0:
        return False, f"unblock exit {p.returncode}: {(p.stderr or p.stdout or '').strip()[:200]}"
    return True, (p.stdout or "").strip()[:120]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--now", default=None, help="ISO8601 override, for testing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--digest", action="store_true", help="force the undated digest")
    ap.add_argument("--no-digest", action="store_true", help="suppress the undated digest")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    db = Path(a.db) if a.db else DEFAULT_DB
    now = parse_when(a.now) if a.now else datetime.now().astimezone()
    if now is None:
        print(f"scheduled-sweep ERROR: unparseable --now {a.now!r}")
        return 0
    if not db.exists():
        return 0

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, assignee FROM tasks WHERE status='scheduled'"
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"scheduled-sweep ERROR: {e}")
        return 0

    due, waiting, undated, failed = [], [], [], []
    for r in rows:
        when, raw = latest_marker(conn, r["id"])
        entry = {"id": r["id"], "title": (r["title"] or "")[:70],
                 "assignee": r["assignee"], "when": raw}
        if when is None:
            undated.append(entry)
        elif when <= now:
            entry["when_iso"] = when.isoformat()
            due.append(entry)
        else:
            entry["when_iso"] = when.isoformat()
            waiting.append(entry)
    conn.close()

    out = []
    for e in due:
        ok, detail = promote(e["id"], e["when"], a.dry_run)
        (out if ok else failed).append(
            f"{'promoted' if ok else 'PROMOTION FAILED'} {e['id']} "
            f"[{e['assignee'] or '-'}] due {e['when']} — {e['title']}"
            + (f"  ({detail})" if detail else ""))
        e["promoted"] = ok

    # Undated cards: weekly, not daily. This is the failure mode the pattern
    # creates, so it must be visible — but never so often it becomes noise.
    # Once per DAY at most, even though the sweep runs hourly — otherwise the
    # weekly digest would print 24 times every Monday, which is the exact noise
    # this whole design exists to remove.
    prev = {}
    try:
        prev = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        pass
    today = now.date().isoformat()
    digest_owed = (not a.no_digest and now.weekday() == DIGEST_WEEKDAY
                   and prev.get("last_digest_date") != today)
    show_digest = a.digest or digest_owed
    digest_lines = []
    if undated and show_digest:
        digest_lines.append(
            f"scheduled cards with no usable `scheduled-until:` marker "
            f"({len(undated)}) — these will never wake on their own:")
        for e in undated:
            digest_lines.append(
                f"  {e['id']} [{e['assignee'] or '-'}] "
                f"{'unparseable: ' + repr(e['when']) if e['when'] else 'no marker'} — {e['title']}")

    # Heartbeat — so a dead sweep is detectable from outside. fleet-preflight
    # asserts this file's freshness; without it, "nothing was due" and "the
    # sweeper is dead" look identical.
    if not a.dry_run:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({
                "at": time.time(),
                "at_iso": now.isoformat(),
                "scheduled_total": len(rows),
                "promoted": [e["id"] for e in due if e.get("promoted")],
                "waiting": len(waiting),
                "undated": len(undated),
                "failed": len(failed),
                "last_digest_date": today if (digest_owed and undated)
                                    else prev.get("last_digest_date"),
            }, indent=1))
        except Exception as e:  # noqa: BLE001
            failed.append(f"heartbeat write failed: {e}")

    if a.json:
        print(json.dumps({"now": now.isoformat(), "due": due, "waiting": waiting,
                          "undated": undated, "failed": failed}, indent=1))
        return 0

    # SILENT when nothing was due, nothing failed, and no digest is owed.
    for line in out + failed + digest_lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
