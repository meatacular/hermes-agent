#!/usr/bin/env python3
"""workspace-parity-watch — detect builder cards provisioned scratch when they should be worktree.

Observed 2026-09-14 (WP4 decomposition): the auto-decomposer's inline INSERT
at kanban_db_graph.py:283-307 omits project_id and does not set workspace_kind
to worktree for builder children of project-linked parents. A child therefore
inherits workspace_kind=scratch from a scratch planning root, and dispatch
never materialises <repo>/.worktrees/<child-id>.

This is the root cause documented on t_2eaa3a62. This watchdog surfaces the
symptom (mismatched workspace) early, before the card reaches a builder and
blocks on capability. It does NOT fix the mint path — that is a plugin or
kernel fix on t_2eaa3a62.

Checks (silent unless something is wrong):
  1. MINT: active builder cards (bob, karl) with workspace_kind=scratch whose
     parent (via task_links) has project_id IS NOT NULL — the child should
     have inherited the project's worktree.
  2. ORPHAN: active builder cards with workspace_kind=scratch AND
     project_id IS NOT NULL — project_id was set but workspace_kind was not
     upgraded (partial fix, still broken at dispatch).
  3. ROOT: triage/ready/todo cards with workspace_kind=scratch and a
     project-linked ancestor 2+ levels up (indirect inheritance failure).

Breach-only reporting: a card is reported ONCE when first detected, then
stays silent while the same breach persists. Cleared breaches are forgotten,
so a card that fixes itself and later re-breaches is reported again.

`no_agent`, read-only, zero LLM tokens. Exit 0 always.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
DB = HERMES_HOME / "kanban.db"
STATE = HERMES_HOME / "state" / "workspace-parity-watch.json"

BUILDERS = ("bob", "karl")
ACTIVE = ("todo", "ready", "running", "blocked", "triage", "scheduled")


def _read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _write_state(state):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE)
    except Exception:
        pass


def _parent_project_id(conn, task_id: str):
    """Return the parent's project_id, or None if no project-linked parent."""
    row = conn.execute(
        "SELECT p.project_id FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.project_id IS NOT NULL "
        "LIMIT 1",
        (task_id,),
    ).fetchone()
    return row[0] if row else None


def _ancestor_project_id(conn, task_id: str, depth: int = 0):
    """Walk up parents to find a project-linked ancestor (up to 3 levels)."""
    if depth > 3:
        return None
    row = conn.execute(
        "SELECT p.id, p.project_id FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    if row[1]:
        return row[1]
    return _ancestor_project_id(conn, row[0], depth + 1)


def main() -> int:
    if not DB.exists():
        return 0
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
    except Exception as e:
        print(f"workspace-parity-watch ERROR: {e}")
        return 0

    state = _read_state()
    cur_keys = set(state.get("reported", []))
    out = []
    new_keys = []

    def _emit(key: str, line: str) -> None:
        if key not in cur_keys:
            out.append(line)
        new_keys.append(key)

    # Check 1: builder cards with workspace_kind=scratch whose parent is project-linked
    try:
        status_list = ",".join(f"'{s}'" for s in ACTIVE)
        rows = c.execute(
            f"SELECT id, title, assignee, workspace_kind, project_id, status "
            f"FROM tasks "
            f"WHERE assignee IN ('bob', 'karl') "
            f"  AND workspace_kind = 'scratch' "
            f"  AND status IN ({status_list})"
        ).fetchall()
        for r in rows:
            pid = _parent_project_id(c, r["id"])
            if pid:
                _emit(
                    f"mint:{r['id']}",
                    f"  MINT    {r['id']} [{r['assignee']}] workspace=scratch "
                    f"but parent has project_id={pid} — "
                    f"{(r['title'] or '')[:55]}",
                )
    except Exception as e:
        _emit("errors", f"  (mint check failed: {e})")

    # Check 2: builder cards with workspace_kind=scratch BUT project_id is set
    # (partial fix — project_id propagated but workspace_kind wasn't upgraded)
    try:
        rows = c.execute(
            f"SELECT id, title, assignee, workspace_kind, project_id, status "
            f"FROM tasks "
            f"WHERE assignee IN ('bob', 'karl') "
            f"  AND workspace_kind = 'scratch' "
            f"  AND project_id IS NOT NULL "
            f"  AND status IN ({status_list})"
        ).fetchall()
        for r in rows:
            _emit(
                f"orphan:{r['id']}",
                f"  ORPHAN  {r['id']} [{r['assignee']}] workspace=scratch "
                f"but project_id={r['project_id']} is set — "
                f"{(r['title'] or '')[:55]}",
            )
    except Exception as e:
        _emit("errors", f"  (orphan check failed: {e})")

    # Check 3: builder cards with workspace_kind=scratch where an ancestor
    # 2+ levels up has project_id (indirect inheritance failure)
    try:
        for r in c.execute(
            f"SELECT id, title, assignee, status FROM tasks "
            f"WHERE assignee IN ('bob', 'karl') "
            f"  AND workspace_kind = 'scratch' "
            f"  AND project_id IS NULL "
            f"  AND status IN ({status_list})"
        ).fetchall():
            # Skip cards already caught by check 1 (direct parent)
            key = f"mint:{r['id']}"
            if key in new_keys or key in cur_keys:
                continue
            pid = _ancestor_project_id(c, r["id"])
            if pid:
                _emit(
                    f"root:{r['id']}",
                    f"  ROOT    {r['id']} [{r['assignee']}] workspace=scratch "
                    f"but ancestor has project_id={pid} — "
                    f"{(r['title'] or '')[:55]}",
                )
    except Exception as e:
        _emit("errors", f"  (ancestor check failed: {e})")

    if out:
        print("workspace-parity-watch — builder cards in scratch that should be worktree:")
        print("\n".join(out))
        print("  (the auto-decomposer's inline INSERT at kanban_db_graph.py:283-307 omits")
        print("   project_id; fix tracked on t_2eaa3a62 — re-provision the workspace manually")

    _write_state({"reported": sorted(new_keys)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
