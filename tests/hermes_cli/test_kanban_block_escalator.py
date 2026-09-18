"""Overwatch contract for the kanban-block-escalator plugin (rewritten 2026-09-06, O1).

Every fault-signal block spawns ONE fresh Smith (``default``) session with a
situation brief; holds, dependency waits and first transients spawn nothing;
a second overwatch on one card, or a cost breach after the one extension, is a
hard stop that leaves the ceiling marker for escalation-watch and spawns the
RUNDOWN session instead. ``switch`` is never a target.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "kanban-block-escalator"


def _load_plugin_module():
    import_path = _PLUGIN_DIR / "__init__.py"
    assert import_path.exists(), f"plugin source missing: {import_path}"
    spec = importlib.util.spec_from_file_location("kanban_block_escalator_under_test", import_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _board(tmp_path, rows, comments=()):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT, "
                "block_recurrences INTEGER, assignee TEXT, created_by TEXT, tenant TEXT, max_cost REAL, "
                "workspace_kind TEXT, workspace_path TEXT)")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER, error TEXT)")
    con.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)")
    for r in rows:
        con.execute("INSERT INTO tasks (id,title,status,block_kind,block_recurrences,assignee,max_cost) VALUES (?,?,?,?,?,?,?)", r)
    for c in comments:
        con.execute("INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,0)", c)
    con.commit()
    con.close()
    return db


@pytest.fixture
def mod(monkeypatch, tmp_path):
    m = _load_plugin_module()
    m.BRIEF_DIR = tmp_path / "briefs"
    # The per-card overwatch lease writes a real file. Without this the suite dropped fixture
    # claims (t_cap, t_env, t_ext, t_c1) into ~/.hermes/state/overwatch — live state, named after
    # cards that do not exist — and a re-run could be suppressed by its own leftover lease.
    monkeypatch.setattr(m, "CLAIM_DIR", tmp_path / "claims")
    monkeypatch.setattr(m, "_hard_ceiling", lambda: 1.50)
    return m


def test_overwatch_is_smith_never_switch_or_a_worker(mod):
    assert mod.OVERWATCH == "default"
    assert mod.OVERWATCH not in ("switch", "bob", "rodge", "karl", "steve-o", "jobsy")
    assert mod.COST_ASSESSOR == mod.OVERWATCH  # Steve-o no longer adjudicates spend


def test_trigger_table():
    m = _load_plugin_module()
    t = lambda kind, rec=0: m.should_trigger({"block_kind": kind, "block_recurrences": rec})[0]
    assert t("cost_cap") and t("capability")
    assert not t("operator_hold") and not t("dependency") and not t("scheduled")
    assert not t("transient", 0) and t("transient", 1)
    # freeze-20260918: `needs_input` is a worker asking a QUESTION, not a fault, and it was
    # 113 of 466 blocks in the week to 18 Sep — roughly a quarter of every overwatch session
    # spawned, with supervision at 27% of fleet spend. It never triggers now, at any
    # recurrence. Reporting is unchanged (escalation-watch, stalled-card-watch still see it);
    # what stopped is paying a supervisor session to read it.
    assert not t("needs_input", 0)
    assert not t("needs_input", 1)
    assert not t("needs_input", 9)


def test_fault_block_spawns_one_smith_session_with_brief(mod, monkeypatch, tmp_path):
    db = _board(tmp_path, [("t_cap", "Build X", "blocked", "capability", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    mod.on_block(task_id="t_cap", assignee="bob", reason="workspace is empty")
    assert len(spawned) == 1
    # The spawn is wrapped in `/bin/sh -c <trap> overwatch <claim> <hermes> -p <profile> …` so the
    # lease is released on exit or signal. Assert on the flag, not on a fixed index: pinning argv
    # position made this test fail on a change that kept the behaviour exactly (2026-09-15).
    argv = spawned[0]
    assert argv[argv.index("-p") + 1] == "default"
    prompt = spawned[0][-1]
    assert prompt.startswith("OVERWATCH:") and "overwatch:" in prompt and "set-cap" in prompt
    assert "Build X" in prompt  # the brief is inline
    assert list((tmp_path / "briefs").glob("t_cap-*.md"))


def test_overwatch_child_is_not_spawned_as_a_kanban_worker(mod, monkeypatch, tmp_path):
    """2026-09-07, t_125dfa35 run 1143.

    ``on_block`` runs inside the blocked worker's own process, so ``os.environ``
    carries ``HERMES_KANBAN_TASK``/``HERMES_KANBAN_RUN_ID``. Inheriting them made
    the overwatch assessor look like a kanban worker to
    ``agent/kanban_checkpoint.py``: it got the per-turn ``[checkpoint]`` reminder
    and the forced terminal-only finalize turn, and was steered toward a board
    call overwatch must never make. The assessor's env must carry the overwatch
    marker and NEITHER worker var.
    """
    db = _board(tmp_path, [("t_env", "Build X", "blocked", "capability", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_env")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1143")
    envs = []
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda argv, **kw: envs.append(kw.get("env") or {}))
    mod.on_block(task_id="t_env", assignee="bob", reason="workspace is empty")
    assert len(envs) == 1
    child = envs[0]
    assert child.get("HERMES_OVERWATCH_TASK") == "t_env"
    assert "HERMES_KANBAN_TASK" not in child
    assert "HERMES_KANBAN_RUN_ID" not in child
    # Negative control: the parent process keeps its own env — the strip is on
    # the child's copy only, so this test cannot pass by mutating os.environ.
    import os as _os
    assert _os.environ.get("HERMES_KANBAN_TASK") == "t_env"


def test_overwatch_child_is_signed_as_the_assessor(mod, monkeypatch, tmp_path):
    """2026-09-15, card t_56500e82.

    The same env inheritance carried ``HERMES_PROFILE``. ``-p <assessor>`` picks the child's home
    — measured, overwatch spend lands in root's ledger every time — but it does not rewrite
    ``os.environ``, and ``tools/kanban_tools.py`` signs a comment with
    ``os.environ.get("HERMES_PROFILE") or "worker"``. So an overwatch ruling spawned from a blocked
    bob worker was authored **bob**: the assessor signing as the worker it is overruling, in a
    comment that is injected verbatim into the next worker's system prompt.

    The variable must be SET to the assessor, not removed — removing it signs the ruling "worker".
    """
    db = _board(tmp_path, [("t_prof", "Build X", "blocked", "capability", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_PROFILE", "rodge")          # the blocked worker's own profile
    monkeypatch.setenv("HERMES_PROFILE_NAME", "rodge")
    envs = []
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda argv, **kw: envs.append(kw.get("env") or {}))
    mod.on_block(task_id="t_prof", assignee="bob", reason="workspace is empty")
    assert len(envs) == 1
    child = envs[0]
    assert child.get("HERMES_PROFILE") == mod.OVERWATCH == "default"
    # Both readers: kanban_tools signs from HERMES_PROFILE, the CLI's _profile_author prefers
    # HERMES_PROFILE_NAME. Fixing one and not the other leaves half the board misattributed.
    assert child.get("HERMES_PROFILE_NAME") == mod.OVERWATCH
    # They must be PRESENT, not merely different: an absent var signs the ruling "worker".
    assert "HERMES_PROFILE" in child and "HERMES_PROFILE_NAME" in child
    # Negative control: the parent keeps its own value, so this cannot pass by mutating os.environ.
    import os as _os
    assert _os.environ.get("HERMES_PROFILE") == "rodge"


def test_hold_dependency_and_first_transient_spawn_nothing(mod, monkeypatch, tmp_path):
    db = _board(tmp_path, [("t_hold", "x", "blocked", "operator_hold", 0, "bob", 1.0),
                           ("t_dep", "x", "todo", "dependency", 0, "bob", 1.0),
                           ("t_tr", "x", "blocked", "transient", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    for tid in ("t_hold", "t_dep", "t_tr"):
        mod.on_block(task_id=tid, assignee="bob", reason="whatever")
    assert spawned == []


def test_second_transient_triggers(mod, monkeypatch, tmp_path):
    db = _board(tmp_path, [("t_tr2", "x", "blocked", "transient", 1, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    mod.on_block(task_id="t_tr2", assignee="bob", reason="flaky again")
    assert len(spawned) == 1


def test_second_overwatch_on_one_card_is_a_hard_stop(mod, monkeypatch, tmp_path):
    # freeze-20260918: fixture kind moved needs_input -> capability. This test is about the
    # SECOND overwatch on one card being a hard stop; needs_input no longer triggers a first
    # one, so it can no longer carry the case. capability is still a fault and still triggers.
    db = _board(tmp_path, [("t_two", "x", "blocked", "capability", 0, "bob", 1.0)],
                comments=[("t_two", "default", "overwatch: reassigned"), ("t_two", "default", "overwatch: split")])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned, marked = [], []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(mod, "_mark_ceiling", lambda tid, reason, why: marked.append((tid, why)))
    mod.on_block(task_id="t_two", assignee="bob", reason="still stuck")
    assert marked and marked[0][0] == "t_two"
    assert len(spawned) == 1 and spawned[0][-1].startswith("HARD STOP")
    assert "rundown:" in spawned[0][-1]


def test_cost_breach_after_extension_is_a_hard_stop(mod, monkeypatch, tmp_path):
    db = _board(tmp_path, [("t_ext", "x", "blocked", "cost_cap", 0, "bob", 1.5)],
                comments=[("t_ext", "default", "cost-extension: $1.00 -> $1.50 by default")])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned, marked = [], []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(mod, "_mark_ceiling", lambda tid, reason, why: marked.append(why))
    mod.on_block(task_id="t_ext", assignee="bob", reason="cumulative spend $1.52 exceeded max_cost $1.50")
    assert marked and ("extension" in marked[0] or "ceiling" in marked[0])
    assert spawned[0][-1].startswith("HARD STOP")


def test_first_cost_breach_is_ordinary_overwatch(mod, monkeypatch, tmp_path):
    """Negative control for the hard stop: same card, no extension yet -> overwatch prompt."""
    db = _board(tmp_path, [("t_c1", "x", "blocked", "cost_cap", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned, marked = [], []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(mod, "_mark_ceiling", lambda *a: marked.append(a))
    mod.on_block(task_id="t_c1", assignee="bob", reason="cumulative spend $1.02 exceeded max_cost $1.00")
    assert marked == [] and spawned[0][-1].startswith("OVERWATCH:")


def test_not_blocked_spawns_nothing(mod, monkeypatch, tmp_path):
    db = _board(tmp_path, [("t_run", "x", "running", None, 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    mod.on_block(task_id="t_run", assignee="bob", reason="x")
    assert spawned == []


def test_overwatch_prompt_contains_idempotency_instruction(mod, monkeypatch, tmp_path):
    """2026-09-14, t_d8c477dd: the overwatch prompt must instruct Smith to use
    idempotency_key on kanban_create so concurrent sessions don't mint duplicates."""
    db = _board(tmp_path, [("t_idem", "Build X", "blocked", "capability", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    mod.on_block(task_id="t_idem", assignee="bob", reason="workspace is empty")
    assert len(spawned) == 1
    prompt = spawned[0][-1]
    assert "IDEMPOTENCY:" in prompt, "prompt must contain the IDEMPOTENCY section"
    assert "idempotency_key" in prompt, "prompt must mention idempotency_key parameter"
    assert "t_idem" in prompt, "prompt must reference the source task id"
    assert "kanban_create" in prompt, "prompt must mention kanban_create"
    assert '<title>' in prompt or 'title' in prompt, "prompt must explain how to derive the key from the title"


def test_idempotency_slug_normalizes_correctly(mod):
    """The slug helper produces deterministic, filesystem-safe idempotency key suffixes."""
    assert mod._idempotency_slug("Consolidate artifact of record") == "consolidate-artifact-of-record"
    assert mod._idempotency_slug("Bob C: read API, merge/split, audit") == "bob-c-read-api-merge-split-audit"
    assert mod._idempotency_slug("") == ""
    assert len(mod._idempotency_slug("a" * 100)) == 60
    # Identical titles produce identical slugs
    assert mod._idempotency_slug("Fix the thing") == mod._idempotency_slug("Fix the thing")
    # Different titles produce different slugs
    assert mod._idempotency_slug("Fix A") != mod._idempotency_slug("Fix B")
