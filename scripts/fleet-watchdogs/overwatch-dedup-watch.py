#!/usr/bin/env python3
"""overwatch-dedup-watch — no_agent watchdog (2026-09-14, t_d8c477dd).

Detects N > 1 non-archived cards with idempotency_key starting with
"overwatch-" that share the same source task id, indicating concurrent
overwatch sessions minted duplicate remediation cards.

This is the safety net (extension point 2) for the prevention mechanism in
the kanban-block-escalator plugin, which instructs overwatch sessions to use
idempotency_key on kanban_create. If two sessions used different keys for the
same source card (e.g. they picked different titles), the idempotency check
in create_task doesn't dedup them — this watchdog flags that gap.

Silent when clean. Reports only when a duplicate group is found.

KNOWN_DUPLICATE_GROUPS: resolved duplicates from the 2026-09-14 WP5 incident
that should not be re-flagged.
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# Idempotency key format: overwatch-{source_task_id}-{title_slug}
KEY_PATTERN = re.compile(r"^overwatch-([a-z0-9_]+)-")

# Known resolved duplicate groups (will not appear if already archived,
# but kept for safety in case any are still non-terminal).
KNOWN_SOURCE_IDS: set[str] = set()

MIN_AGE_SECONDS = 120


def _board_db() -> str:
    return os.environ.get(
        "HERMES_KANBAN_DB",
        str(Path.home() / ".hermes" / "kanban.db"),
    )


def _source_task_id(key: str) -> str | None:
    m = KEY_PATTERN.match(key)
    return m.group(1) if m else None


def run() -> int:
    db_path = _board_db()
    if not os.path.isfile(db_path):
        return 0

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        now = int(time.time())

        # Find non-terminal cards with an overwatch-style idempotency_key
        rows = con.execute(
            """
            SELECT id, title, status, assignee, idempotency_key, created_at
            FROM tasks
            WHERE status NOT IN ('done', 'archived')
              AND idempotency_key IS NOT NULL
              AND idempotency_key LIKE 'overwatch-%'
            ORDER BY idempotency_key, created_at
            """
        ).fetchall()

        if not rows:
            return 0

        # Group by source task id (extracted from the key)
        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            src = _source_task_id(r["idempotency_key"])
            if src:
                groups.setdefault(src, []).append(r)

        findings = []
        for src_id, cards in sorted(groups.items()):
            if src_id in KNOWN_SOURCE_IDS:
                continue
            if len(cards) < 2:
                continue

            # Filter to mature cards
            mature = [
                c for c in cards
                if (now - (c["created_at"] or 0)) >= MIN_AGE_SECONDS
            ]
            if len(mature) < 2:
                continue

            # Check if any have different idempotency keys
            keys = {c["idempotency_key"] for c in mature}
            if len(keys) < 2:
                # All have the same key — idempotency worked correctly,
                # the duplicate was prevented.
                continue

            lines = [
                f"OVERWATCH-DEDUP: {len(mature)} non-archived cards for "
                f"source {src_id} with different idempotency keys "
                f"({len(keys)} distinct keys)",
            ]
            for c in mature:
                lines.append(
                    f"  {c['id']} [{c['status']}] {c['assignee'] or '?'} "
                    f"key={c['idempotency_key']} | "
                    f"{(c['title'] or '')[:60]}"
                )
            findings.append("\n".join(lines))

        if findings:
            # Write to stderr so cron captures it as a warning
            print(
                "OVERWATCH-DEDUP-WATCH: duplicate remediation cards detected "
                "(different idempotency keys for the same source card)",
                file=sys.stderr,
            )
            for f in findings:
                print(f, file=sys.stderr)
            # Also write to stdout with a machine-readable summary
            print(
                json.dumps({
                    "watchdog": "overwatch-dedup-watch",
                    "status": "DUPLICATES_FOUND",
                    "groups": len(findings),
                    "count": sum(len(g) for g in findings),
                    "sources": sorted(set(
                        _source_task_id(r["idempotency_key"])
                        for cards in groups.values()
                        for r in cards
                    )),
                })
            )
            return 1

        return 0

    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(run())
