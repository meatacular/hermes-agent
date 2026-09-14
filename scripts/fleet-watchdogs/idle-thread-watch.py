#!/usr/bin/env python3
"""Idle-thread reminder watchdog (no_agent, silent unless something is idle).

Solves the long-lived-thread context-bleed cost leak: a persistent thread
(group / dm / bot chat) that goes quiet for a long time still re-reads its
whole history on every resume. We catch it early and hand the operator a
decision point (manual archive+handover vs keep going) instead of paying for
re-reading day-old context forever.

Wire-up: `no_agent` cron, e.g. every 30m. Silent-on-healthy: empty stdout =
nothing idle -> no action. Non-empty stdout = delivered to the operator /
wakes the orchestrator to act.

Usage:
    idle-thread-watch.py                  # report only (cron default)
    idle-thread-watch.py --audit          # always print, for smoke-testing
    idle-thread-watch.py --json           # machine-readable, for the plugin
"""

import json
import os
import sqlite3
import sys
import time

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if not os.path.isdir(os.path.join(HERMES_HOME, "profiles")):
    _up = os.path.dirname(os.path.dirname(HERMES_HOME))
    if os.path.isdir(os.path.join(_up, "profiles")):
        HERMES_HOME = _up

# Persistent-thread chat types we care about. Worker runs (kanban) are fresh
# sessions per run and never show up here with a chat_type, so they are
# naturally excluded by this filter.
PERSISTENT_TYPES = ("group", "dm", "bot", "channel")

# Minimum idle before we flag. 24h chosen as the operator's asked-for default.
IDLE_SECONDS = 24 * 3600

# Minimum message count / cost to be worth flagging - skip trivial threads.
MIN_MESSAGES = 30
MIN_COST_USD = 0.05

# Re-remind window: a thread is only re-flagged if it hasn't been reminded in
# the last REMIND_EVERY seconds. Stopping the same dormant thread from posting
# every cron tick.
REMIND_EVERY = 24 * 3600
_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      ".idle-thread-watch.state.json")


def _db_paths():
    """Yield (profile_name, state_db_path) for the fleet, default first."""
    paths = [("default", os.path.join(HERMES_HOME, "state.db"))]
    profiles_dir = os.path.join(HERMES_HOME, "profiles")
    if os.path.isdir(profiles_dir):
        for name in sorted(os.listdir(profiles_dir)):
            p = os.path.join(profiles_dir, name, "state.db")
            if os.path.isfile(p):
                paths.append((name, p))
    return paths


def _idle_sessions(db):
    """Return up-to-date persistent sessions that have gone idle too long."""
    out = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT id, chat_type, chat_id, COALESCE(display_name,'')  AS display_name,
                       COALESCE(title,'') AS title, message_count, input_tokens,
                       cache_read_tokens, estimated_cost_usd, parent_session_id,
                       archived, last_activity_at
                FROM sessions
                WHERE chat_type IS NOT NULL AND chat_type != ''
                  AND archived = 0
                  AND last_activity_at IS NOT NULL
                """
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return out

    now = time.time()
    for r in rows:
        if r["chat_type"] not in PERSISTENT_TYPES:
            continue
        idle = now - r["last_activity_at"]
        if idle < IDLE_SECONDS:
            continue
        if (r["message_count"] or 0) < MIN_MESSAGES:
            continue
        cost = r["estimated_cost_usd"] or 0.0
        if cost < MIN_COST_USD:
            continue
        out.append({
            "profile": None,  # filled by caller
            "session_id": r["id"],
            "chat_type": r["chat_type"],
            "chat_id": r["chat_id"] or "",
            "display_name": r["display_name"] or r["title"] or r["id"][:22],
            "messages": r["message_count"] or 0,
            "input_tokens": r["input_tokens"] or 0,
            "cache_read_tokens": r["cache_read_tokens"] or 0,
            "estimated_cost_usd": round(cost, 3),
            "idle_seconds": int(idle),
            "idle_human": _human_duration(idle),
            "has_parent": bool(r["parent_session_id"]),
            "archived": bool(r["archived"]),
        })
    return out


def _human_duration(secs):
    secs = int(secs)
    days, rem = divmod(secs, 86400)
    hrs, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hrs or days:
        parts.append(f"{hrs}h")
    if not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def _load_state():
    try:
        with open(_STATE, "r") as fh:
            return json.load(fh).get("reminded", {})
    except Exception:
        return {}


def _save_state(reminded):
    try:
        with open(_STATE, "w") as fh:
            json.dump({"reminded": reminded}, fh, indent=2)
    except Exception:
        pass


def collect():
    results = []
    for profile, db in _db_paths():
        for s in _idle_sessions(db):
            s["profile"] = profile
            results.append(s)
    # dedupe by session id across profiles (safety)
    seen, uniq = set(), []
    for s in sorted(results, key=lambda x: -x["estimated_cost_usd"]):
        if s["session_id"] in seen:
            continue
        seen.add(s["session_id"])
        uniq.append(s)
    return uniq


def run(audit=False, as_json=False):
    flagged = collect()
    now = time.time()

    if as_json:
        return json.dumps({"idle_threads": flagged}, indent=2)

    reminded = _load_state()
    fresh = [s for s in flagged
             if s["session_id"] not in reminded
             or (now - reminded[s["session_id"]]) >= REMIND_EVERY]

    if not fresh:
        return ""

    for s in fresh:
        reminded[s["session_id"]] = now
    _save_state(reminded)

    lines = []
    for s in fresh:
        lines.append(
            f"[idle-thread] {s['profile']}/{s['chat_type']} "
            f"'{s['display_name']}' idle {s['idle_human']} "
            f"({s['messages']} msgs, ${s['estimated_cost_usd']:.2f}). "
            f"Archive+handover to a fresh session, or keep going."
        )
    return "\n".join(lines)


if __name__ == "__main__":
    out = run(audit="--audit" in sys.argv, as_json="--json" in sys.argv)
    if out:
        print(out)