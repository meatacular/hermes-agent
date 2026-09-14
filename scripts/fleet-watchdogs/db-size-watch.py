#!/usr/bin/env python3
"""state.db size watchdog (no_agent, silent unless over threshold).

Complements the retention ladder (auto_archive 7d + auto_prune 18mo) with a
size alarm: if the fleet's state.db files sum past WARN_GB, print a warning
(non-empty stdout = delivered to the operator / wakes the orchestrator).

Why 1 GiB and not 10: hermes doctor already flags state.db at 1 GiB
(STATE_DB_SIZE_WARN_BYTES), and the sessions docs cite ~384 MB as where FTS5
inserts + /resume listing start to degrade.  A 10 GiB alarm would fire years
after search recall is already slow.

Silent-on-healthy, like idle-thread-watch.py: empty stdout = nothing to do.

Usage:
    db-size-watch.py                  # report only if over threshold (cron default)
    db-size-watch.py --audit          # always print a one-line summary
    db-size-watch.py --threshold-gb 10  # override (not recommended)
"""

import argparse
import os
import sqlite3
import sys

WARN_GB = 1

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if not os.path.isdir(os.path.join(HERMES_HOME, "profiles")):
    _up = os.path.dirname(os.path.dirname(HERMES_HOME))
    if os.path.isdir(os.path.join(_up, "profiles")):
        HERMES_HOME = _up


def _db_paths():
    """Yield (profile_name, state_db_path) for the fleet, default first."""
    yield ("default", os.path.join(HERMES_HOME, "state.db"))
    profiles_dir = os.path.join(HERMES_HOME, "profiles")
    if os.path.isdir(profiles_dir):
        for name in sorted(os.listdir(profiles_dir)):
            p = os.path.join(profiles_dir, name, "state.db")
            if os.path.isfile(p):
                yield (name, p)


def _logical_size(db):
    """SQLite logical size (page_count * page_size) in bytes.

    Prefer the DB's own PRAGMA over os.path.getsize: page_count * page_size is
    the number that VACUUM / optimize-storage actually care about, and it stays
    correct whether or not a WAL is currently holding uncheckpointed pages.

    Deliberately does NOT add the WAL.  The WAL is capped (journal_size_limit)
    and checkpoints back into the main file, so adding it would double-count
    not-yet-checkpointed pages and over-report after any large write burst
    (e.g. a VACUUM).  Page count already reflects the true footprint.
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("PRAGMA page_count").fetchone()
            psize = con.execute("PRAGMA page_size").fetchone()
            if row and psize:
                return row[0] * psize[0]
        finally:
            con.close()
    except Exception:
        pass
    try:
        return os.path.getsize(db)
    except OSError:
        return 0


def _human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--threshold-gb", type=float, default=WARN_GB)
    args = ap.parse_args()

    threshold = args.threshold_gb * 1024 * 1024 * 1024
    sizes = []
    total = 0
    for name, db in _db_paths():
        s = _logical_size(db)
        sizes.append((name, db, s))
        total += s

    if args.audit:
        for name, db, s in sizes:
            print(f"{name:10s} {_human(s):>10s}  {db}")
        print(f"{'TOTAL':10s} {_human(total):>10s}")
        return

    if total > threshold:
        print(
            f"state.db total {_human(total)} exceeds {args.threshold_gb:.0f} GiB. "
            f"Consider 'hermes sessions optimize-storage' (reclaims ~60% of the "
            f"legacy FTS index) and/or tightening sessions.retention_days."
        )
        # Surface the biggest offenders for triage.
        for name, db, s in sorted(sizes, key=lambda x: -x[2])[:3]:
            print(f"  {name:10s} {_human(s)}  {db}")


if __name__ == "__main__":
    main()
