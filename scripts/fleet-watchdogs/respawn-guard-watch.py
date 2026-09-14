#!/usr/bin/env python3
"""respawn-guard-watch — a card the dispatcher keeps REFUSING to spawn (2026-09-14).

The gap this closes, measured on 2026-09-13: card ``t_22502d22`` sat in ``ready`` for 21
minutes while the dispatcher logged ``respawn_guarded {"reason": "active_pr"}`` once a
minute, 24 times. Nothing noticed. Not a block, so ``kanban-block-escalator`` never fired
and Agent Smith was never handed the card. Not ``status='todo'``, so ``stalled-card-watch``'s
undispatched arm could not see it at any threshold. And NOTHING in ~/.hermes/scripts or
plugins/ read the ``respawn_guarded`` event at all. A human found it.

A guarded card is the one board state that is invisible from the board: ``ready``, assigned,
unclaimed, with a fresh-looking row and no error anywhere. This reports it.

Silent unless something is wrong, zero tokens, stdlib only (cron runs the SYSTEM python3,
where ``import yaml`` raises while the checks list is being built).

Thresholds: a card is reported once it has been refused MIN_GUARDS consecutive times with no
spawn in between and the newest refusal is inside FRESH_S (so an archived/long-settled card
does not re-surface). Consecutive means "since the last claim/spawn" — a card that spawned and
was later guarded again starts a new streak, because that is a new refusal, not the old one.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# Trap 15: never resolve a path from a Cowork /sessions mount that dies with the session.
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":      # invoked under a worker's home
    HERMES_HOME = HERMES_HOME.parent.parent
DB = HERMES_HOME / "kanban.db"
STATE = HERMES_HOME / "state" / "respawn-guard-watch.json"

MIN_GUARDS = int(os.environ.get("RESPAWN_MIN_GUARDS", "3"))
FRESH_S = int(os.environ.get("RESPAWN_FRESH_S", "3600"))
TERMINAL = ("done", "archived")

# What the operator can actually DO about each reason. The dispatcher writes the reason and
# then says nothing; a reason with no exit is how a card sits for 21 minutes.
EXITS = {
    "active_pr": (
        "a GitHub PR URL appears in a comment on this card from the last 24h. The guard is a "
        "REGEX OVER COMMENT TEXT, not a live PR check — closing the PR does not clear it, and "
        "writing ABOUT a PR sets it. There is no override. Exits: mint a fresh card with the "
        "reviewer's outstanding-checklist copied into the body and archive this one, or wait "
        "out the 24h window. Do not edit the reviewer's comment."),
    "blocker_auth": (
        "last_failure_error matches the quota/auth pattern — retrying cannot help. Fix the "
        "credential or the quota, then clear last_failure_error or re-queue the card."),
    "rate_limit_cooldown": (
        "the last run was rate-limited and the cooldown has not elapsed. This one self-clears; "
        "no action unless it repeats for hours."),
    "recent_success": (
        "a completed run inside the 1h window and no re-queue event after it. Drag the card "
        "ready->triage->ready (or unblock it) to record a deliberate re-run."),
}


def emit(lines):
    print("\n".join(lines))


def main():
    if not DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        print(f"respawn-guard-watch: cannot open kanban.db: {e}", file=sys.stderr)
        return 0

    now = int(time.time())
    findings, fingerprint = [], []
    try:
        rows = con.execute(
            "SELECT id, title, assignee, status FROM tasks "
            "WHERE status NOT IN (?, ?)", TERMINAL).fetchall()
    except sqlite3.Error:
        return 0

    for t in rows:
        try:
            evs = con.execute(
                "SELECT kind, payload, created_at FROM task_events "
                "WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 200",
                (t["id"],)).fetchall()
        except sqlite3.Error:
            continue
        streak, newest, reason = 0, None, None
        for e in evs:
            if e["kind"] == "respawn_guarded":
                streak += 1
                if newest is None:
                    newest = int(e["created_at"] or 0)
                    try:
                        reason = (json.loads(e["payload"] or "{}") or {}).get("reason")
                    except (ValueError, TypeError):
                        reason = None
            elif e["kind"] in ("spawned", "claimed"):
                break           # the streak is only what happened since the last real spawn
        if streak < MIN_GUARDS or newest is None or (now - newest) > FRESH_S:
            continue
        age = (now - newest) // 60
        findings.append(
            f"  GUARDED {t['id']} [{t['assignee'] or '-'}] status={t['status']} "
            f"refused {streak}x, reason={reason or '?'}, last {age} min ago\n"
            f"          {(t['title'] or '')[:78]}\n"
            f"          {EXITS.get(reason or '', 'unknown guard reason — read check_respawn_guard() in kanban_db_dispatch.py')}")
        fingerprint.append(f"{t['id']}:{reason}:{streak // 5}")

    con.close()
    if not findings:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"at": now, "fingerprint": []}))
        return 0

    # Dedup like the other watchdogs: an unchanged breach set stays silent, so a card that is
    # genuinely waiting out a 24h window does not shout every five minutes.
    prev = []
    try:
        prev = (json.loads(STATE.read_text()) or {}).get("fingerprint") or []
    except (OSError, ValueError):
        pass
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"at": now, "fingerprint": fingerprint}))
    if sorted(fingerprint) == sorted(prev):
        return 0

    emit([f"Dispatcher is refusing to spawn {len(findings)} card(s) — this is NOT a block, so "
          f"nothing escalates it:"] + findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
