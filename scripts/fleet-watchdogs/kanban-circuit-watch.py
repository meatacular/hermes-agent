#!/usr/bin/env python3
"""Global dispatch circuit-breaker alert watchdog.

Why this exists
---------------
On 2026-08-30 the OpenRouter monthly-key 403 bounced every worker at spawn
inside ~2s; the dispatcher retried 22 cards into the same wall before a human
parked them. The kanban dispatcher now trips a global circuit breaker on that
signature and records a single "tripped" (and later a single "resumed") alert
in the circuit outbox file.

This watchdog drains that outbox ONCE per break and hands the alert to the
fleet's scheduled-agent Slack channel (the same budget-watch uses), then clears
the file so a paused breaker does not nag every tick. Empty outbox = silent
tick, matching every other `no_agent` fleet watchdog.

`no_agent` cron script: no LLM call, no tokens, $0. Silent unless there is an
alert to deliver.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
try:
    from comms_style import clean
except Exception:  # pragma: no cover
    def clean(s):
        return s

# Dispatcher state lives under the fleet's shared kanban root. The default
# board keeps the legacy ~/.hermes/kanban/ path; other boards nest under
# kanban/boards/<slug>/ (mirrors worker_logs_dir / _circuit_outbox_path).
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
OUTBOXES = [
    HERMES_HOME / "kanban" / "circuit-outbox.json",
    HERMES_HOME / "kanban" / "boards",
]
BOARD = os.environ.get("HERMES_KANBAN_BOARD", "").strip()


def _candidate_paths():
    paths = []
    # Explicit board selection wins; otherwise default + any board dirs.
    if BOARD:
        if BOARD == "default":
            paths.append(HERMES_HOME / "kanban" / "circuit-outbox.json")
        else:
            paths.append(HERMES_HOME / "kanban" / "boards" / BOARD / "circuit-outbox.json")
        return paths
    root = HERMES_HOME / "kanban"
    if (root / "circuit-outbox.json").exists():
        paths.append(root / "circuit-outbox.json")
    for board_dir in sorted((HERMES_HOME / "kanban" / "boards").glob("*/")):
        p = board_dir / "circuit-outbox.json"
        if p.exists():
            paths.append(p)
    return paths


def main():
    lines = []
    drained = []
    for path in _candidate_paths():
        try:
            data = json.loads(path.read_text(errors="ignore") or "{}")
        except Exception:
            data = {}
        msgs = data.get("messages", [])
        if not isinstance(msgs, list) or not msgs:
            continue
        for m in msgs:
            kind = (m.get("kind") or "alert").replace("_", " ")
            text = m.get("message") or ""
            at = m.get("at")
            when = ""
            if at:
                import datetime
                when = f" ({datetime.datetime.fromtimestamp(int(at)).astimezone().strftime('%H:%M:%S')})"
            lines.append(f"{kind}{when}: {text}")
        drained.append(path)

    if lines:
        try:
            HERMES_HOME.mkdir(parents=True, exist_ok=True)
            # Write an empty file for the drained outbox so next tick sees
            # nothing pending (a present-but-empty file is fine — _queue_circuit_alert
            # treats a non-list/missing messages the same as empty).
            for p in drained:
                p.write_text(json.dumps({"messages": []}))
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"circuit-watch: failed to drain outbox: {exc}\n")
        print(clean("\n".join(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())