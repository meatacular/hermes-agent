#!/usr/bin/env python3
"""Charter §4 estimate at the mint path — as a WATCHDOG, not a core patch.

Re-expresses `64ab4fc8a` ("charter §4 estimate at the mint path"), which put this
inside `kanban_db.create_task` and `kanban_decompose`. The behaviour was right; the
location was not — per the 2026-09-09 decision, upstream's kanban stays the substrate
and policy lives outside it. Reverted from core 2026-09-12 and re-expressed here.

A sweep is actually a BETTER fit than the core hook was, for two reasons:

  * it covers EVERY mint path by construction — the CLI, the kanban_create tool, the
    auto-decomposer (~70% of cards), the dashboard API, `Deploy:` follow-ups and the
    swarm — without needing a hook at each one, and without a new one escaping it;
  * the number it feeds (the charter §2 points-coverage metric, via
    `scripts/cost-ledger.py` PTS_RE) is read long after the card is minted, so a
    sweep every few minutes is indistinguishable from a write at creation.

Points travel as a `points-estimate: N` COMMENT, not a column — the ledger and the
metric both read comments, so a column would be a second source of truth that moves
no number. Same convention as `scheduled-until:` for date waits.

Silent unless it acted. Read-only against the board except for the comments it posts,
and it posts them through the `hermes kanban` CLI rather than raw SQL — a watchdog
minting by direct INSERT is how nine uncapped cards appeared on 2026-09-06.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
DB = Path(os.environ.get("POINTS_MINT_WATCH_DB") or HERMES_HOME / "kanban.db")
HERMES = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes"

# Must match scripts/cost-ledger.py PTS_RE, or this writes a comment the metric
# cannot see — which is the failure mode the whole exercise exists to avoid.
PTS_RE = re.compile(r"points-estimate[^0-9]*([0-9]+)", re.I)

PLACEHOLDER = 1
NOTE = (
    "auto-points: §4 placeholder — written by points-mint-watch because this "
    "card carried no estimate. The specifier replaces it on first touch by posting the "
    "real estimate; a later estimate comment wins over this one."
)


def comment_body(points: int = PLACEHOLDER) -> str:
    return f"points-estimate: {points}\n{NOTE}"


def unestimated(conn: sqlite3.Connection, since_epoch: int) -> list[tuple[str, str]]:
    """Cards minted since `since_epoch` that carry no points estimate in any comment."""
    rows = conn.execute(
        "SELECT id, COALESCE(title,'') FROM tasks "
        "WHERE COALESCE(created_at,0) >= ? AND status != 'archived' "
        "ORDER BY created_at",
        (since_epoch,),
    ).fetchall()
    out = []
    for tid, title in rows:
        bodies = conn.execute(
            "SELECT COALESCE(body,'') FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()
        if not any(PTS_RE.search(b[0]) for b in bodies):
            out.append((tid, title))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=60,
                    help="look back this far for freshly minted cards")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not DB.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"points-mint-watch: kanban.db unreadable: {exc}")
        return 0
    try:
        targets = unestimated(conn, int(time.time()) - a.minutes * 60)
    finally:
        conn.close()

    if not targets:
        return 0                      # silent: the normal case

    acted, failed = [], []
    for tid, title in targets:
        if a.dry_run:
            acted.append((tid, title))
            continue
        try:
            r = subprocess.run([str(HERMES), "kanban", "comment", tid, comment_body()],
                               capture_output=True, text=True, timeout=60)
            (acted if r.returncode == 0 else failed).append((tid, title))
        except Exception as exc:  # noqa: BLE001
            failed.append((tid, f"{title} ({exc})"))

    verb = "would write" if a.dry_run else "wrote"
    print(f"[points-mint] {verb} a §4 placeholder estimate on {len(acted)} card(s) "
          f"minted in the last {a.minutes} min")
    for tid, title in acted:
        print(f"  {tid}  {title[:70]}")
    if failed:
        print(f"[points-mint] FAILED on {len(failed)} card(s) — these stay unestimated:")
        for tid, title in failed:
            print(f"  {tid}  {title[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
