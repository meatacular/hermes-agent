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

            # 2026-09-15 CORRECTION. This used to fire whenever two cards from one source
            # carried DIFFERENT idempotency keys — which is the normal, intended case: one
            # escalation legitimately produces several distinct remediation cards. On 2026-09-14
            # it flagged four such cards (t_796c3fab / t_da58d620 from t_5262a7a8, and
            # t_56500e82 / t_ce800bec from t_cff44897), all of them real and all with different
            # subjects, every 15 minutes, and exited 1 each time until failure_streak reached 40
            # and the scheduler was one step from disabling it. A watchdog that cries wolf and
            # then silences itself is worse than no watchdog.
            #
            # A duplicate is two cards that say the SAME THING, not two cards from one parent.
            # So: same key (the idempotency check failed to dedupe), or near-identical titles
            # (two sessions wrote the same card under different keys, which is the shape the
            # idempotency instruction exists to prevent).
            import difflib
            import re as _re

            def _norm(t):
                t = _re.sub(r"^\s*\[[^\]]+\]\s*", "", (t or "").lower())
                return _re.sub(r"[^a-z0-9 ]+", " ", t).split()

            dupes = []
            by_key = {}
            for c in mature:
                by_key.setdefault(c["idempotency_key"], []).append(c)
            for k, cs in by_key.items():
                if len(cs) > 1:
                    dupes.append((f"same idempotency key {k}", cs))
            for i in range(len(mature)):
                for j in range(i + 1, len(mature)):
                    a, b = mature[i], mature[j]
                    if a["idempotency_key"] == b["idempotency_key"]:
                        continue
                    ratio = difflib.SequenceMatcher(
                        None, " ".join(_norm(a["title"])), " ".join(_norm(b["title"]))).ratio()
                    if ratio >= 0.85:
                        dupes.append((f"titles {int(ratio * 100)}% identical", [a, b]))
            if not dupes:
                continue

            for why, cs in dupes:
                lines = [f"OVERWATCH-DEDUP: {len(cs)} cards for source {src_id} — {why}"]
                for c in cs:
                    lines.append(
                        f"  {c['id']} [{c['status']}] {c['assignee'] or '?'} "
                        f"key={c['idempotency_key']} | {(c['title'] or '')[:60]}"
                    )
                findings.append("\n".join(lines))

        if findings:
            print("OVERWATCH-DEDUP-WATCH: duplicate remediation cards detected "
                  "(same key, or two cards saying the same thing)", file=sys.stderr)
            for f in findings:
                print(f, file=sys.stderr)
            print(json.dumps({
                "watchdog": "overwatch-dedup-watch",
                "status": "DUPLICATES_FOUND",
                "groups": len(findings),
                # was `sum(len(g) for g in findings)`, which summed the LENGTH OF EACH STRING and
                # reported "count": 903 for four cards.
                "cards": sum(f.count("\n") for f in findings),
            }))
        # Always 0. A finding is not a failure; see the note above about failure_streak.
        return 0

    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(run())
