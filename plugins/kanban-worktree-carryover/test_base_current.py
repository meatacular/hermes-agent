"""Acceptance tests for the base-current phase (P1-base-current, 2026-09-23).

The defect: ``kanban_db_workspace._ensure_git_worktree`` cuts a NEW branch from
the primary clone's HEAD, unfetched, so whatever branch the live clone is parked
on becomes the base of every new card. The plugin makes the branch exist at
``refs/remotes/origin/<trunk>`` first, so the kernel's existing-branch reuse
checks it out from there.

    (a) test_absent_branch_is_created_at_origin_trunk_and_head_is_untouched
    (b) test_existing_branch_is_left_alone            (NEGATIVE CONTROL)
    (c) test_missing_origin_trunk_is_a_quiet_noop
    (d) test_kill_switch_disables_it                  (NEGATIVE CONTROL)
    (e) test_tenant_trunk_override_is_respected
    +   test_kernel_then_cuts_the_worktree_from_origin_trunch (real kernel)
    +   test_stale_fetch_head_is_only_a_warning
    +   test_hook_runs_base_current_after_carry_over

Run:  env -u HERMES_DELEGATED_CHILD_CONTEXT <venv>/bin/python -m pytest \
          plugins/kanban-worktree-carryover/test_base_current.py -q -p no:randomly
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent

TASK = "t_basecur01"
BRANCH = "proj/t_basecur01-thing"


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "kanban_worktree_carryover_base_current_under_test", _PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check)


def _rev(cwd, ref):
    return _git(cwd, "rev-parse", "--verify", ref).stdout.strip()


def _has_branch(repo, branch):
    return _git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "kanban@example.com")
    _git(path, "config", "user.name", "Kanban Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def _commit(path: Path, name: str, message: str) -> str:
    (path / name).write_text(message + "\n", encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-m", message)
    return _rev(path, "HEAD")


_MINIMAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
  status TEXT, priority INTEGER DEFAULT 0, workspace_kind TEXT, workspace_path TEXT,
  branch_name TEXT, tenant TEXT, created_at INTEGER, created_by TEXT, started_at INTEGER,
  completed_at INTEGER, claim_lock TEXT, claim_expires INTEGER, max_cost REAL,
  block_kind TEXT, project_id TEXT);
CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  profile TEXT, status TEXT, started_at INTEGER, ended_at INTEGER, outcome TEXT);
CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  kind TEXT, payload TEXT, created_at INTEGER);
"""


def _init_schema(db: Path) -> None:
    """The kernel's schema when the kernel imports (the native pytest run);
    a stdlib mirror of the columns this plugin reads otherwise, so the plugin
    logic is testable anywhere ``git`` exists."""
    try:
        from hermes_cli import kanban_db_connect as kbc

        kbc.init_db(db_path=db)
        return
    except Exception:
        pass
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_MINIMAL_SCHEMA)
        conn.commit()
    finally:
        conn.close()


class Parked:
    """One origin, one primary clone whose HEAD is parked on a side branch that
    is AHEAD of origin/main — the live shape of ~/Projects/weroll-app."""

    def __init__(self, tmp_path: Path):
        self.origin = tmp_path / "origin"
        self.clone = tmp_path / "clone"
        self.db = tmp_path / "kanban.db"
        self.tenants = tmp_path / "kanban-tenants.json"
        _init_repo(self.origin)
        _git(tmp_path, "clone", str(self.origin), str(self.clone))
        _git(self.clone, "config", "user.email", "kanban@example.com")
        _git(self.clone, "config", "user.name", "Kanban Test")
        self.trunk_head = _rev(self.clone, "refs/remotes/origin/main")
        _git(self.clone, "checkout", "-b", "parked-feature")
        self.parked_head = _commit(self.clone, "parked.txt", "wip: parked local work")
        assert self.parked_head != self.trunk_head
        self.ws = self.clone / ".worktrees" / TASK
        self.tenants.write_text("{}", encoding="utf-8")

    def write_board(self, branch_name=BRANCH, workspace_kind="worktree"):
        _init_schema(self.db)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, workspace_kind, "
                " workspace_path, branch_name, tenant, created_at, created_by, max_cost, "
                " block_kind, project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (TASK, "[Bob] base-current card", "", "bob", "running", workspace_kind,
                 str(self.ws), branch_name, None, int(time.time()), "jobsy", 1.0, None, "p_x"),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?,?,?,?,?,?)",
                (TASK, "bob", "running", int(time.time()), None, None),
            )
            conn.commit()
        finally:
            conn.close()

    def write_tenants(self, **tenant_fields):
        self.tenants.write_text(json.dumps({
            "_comment": "test",
            "weroll-app": {"id": "p_weroll_app", "slug": "weroll-app",
                           "primary_path": str(self.clone), **tenant_fields},
        }), encoding="utf-8")

    def head_state(self):
        return (_git(self.clone, "symbolic-ref", "HEAD").stdout.strip(), _rev(self.clone, "HEAD"))

    def events(self, kind):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                (TASK, kind)).fetchall()
        finally:
            conn.close()
        return [json.loads(r[0]) for r in rows]

    def provision(self):
        """Run the REAL kernel provisioning against the card's target repo."""
        kb = pytest.importorskip("hermes_cli.kanban_db")
        kbw = pytest.importorskip("hermes_cli.kanban_db_workspace")

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            task = kb.get_task(conn, TASK)
        finally:
            conn.close()
        assert task is not None
        return kbw._resolve_worktree_workspace(task)


@pytest.fixture
def parked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv(plugin.ENV_KILL_SWITCH, raising=False)
    monkeypatch.delenv(plugin.ENV_BASE_CURRENT, raising=False)
    monkeypatch.delenv(plugin.ENV_BASE_CURRENT_MAX_AGE, raising=False)
    p = Parked(tmp_path)
    monkeypatch.setenv(plugin.ENV_TENANTS_PATH, str(p.tenants))
    return p


def _run(p: Parked, env=None):
    return plugin.base_current(TASK, "default", db_path=p.db, env=env or {}, tenants_path=p.tenants)


# (a) -----------------------------------------------------------------------

def test_absent_branch_is_created_at_origin_trunk_and_head_is_untouched(parked):
    parked.write_board()
    before = parked.head_state()
    assert not _has_branch(parked.clone, BRANCH)

    verdict = _run(parked)

    assert verdict["action"] == "based", verdict
    assert verdict["trunk"] == "main" and verdict["trunk_source"] == "origin/HEAD"
    assert _rev(parked.clone, f"refs/heads/{BRANCH}") == parked.trunk_head
    assert _rev(parked.clone, f"refs/heads/{BRANCH}") != parked.parked_head
    # HEAD, index and worktrees untouched: still parked, still the same commit.
    assert parked.head_state() == before == ("refs/heads/parked-feature", parked.parked_head)
    assert _git(parked.clone, "status", "--porcelain").stdout.strip() == ""
    assert _git(parked.clone, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1
    # A plain local ref — no upstream tracking configured.
    assert _git(parked.clone, "config", "--get", f"branch.{BRANCH}.remote", check=False).returncode != 0
    # Recorded on the board, like carry-over.
    events = parked.events("base_current")
    assert len(events) == 1 and events[0]["base"] == parked.trunk_head


def test_kernel_then_cuts_the_worktree_from_origin_trunk(parked):
    """End to end against the REAL kernel: with the branch pre-created, the
    kernel's existing-branch reuse hands the worker origin/main, not the
    parked HEAD it would have used otherwise."""
    parked.write_board()
    _run(parked)
    worktree, branch = parked.provision()
    assert Path(worktree) == parked.ws and branch == BRANCH
    assert _rev(parked.ws, "HEAD") == parked.trunk_head
    assert not (parked.ws / "parked.txt").exists()


def test_without_base_current_the_kernel_cuts_from_parked_head(parked):
    """Faithfulness of the reproduction: the defect is real without the plugin."""
    parked.write_board()
    worktree, _ = parked.provision()
    assert _rev(parked.ws, "HEAD") == parked.parked_head
    assert (parked.ws / "parked.txt").exists()


# (b) NEGATIVE CONTROL --------------------------------------------------------

def test_existing_branch_is_left_alone(parked):
    parked.write_board()
    _git(parked.clone, "branch", BRANCH, "HEAD")          # already exists, at parked HEAD
    own = _rev(parked.clone, f"refs/heads/{BRANCH}")

    verdict = _run(parked)

    assert verdict["action"] == "noop"
    assert verdict["reason"] == "branch already present in target repo"
    assert _rev(parked.clone, f"refs/heads/{BRANCH}") == own   # NOT moved to origin/main
    assert parked.events("base_current") == []


# (c) -----------------------------------------------------------------------

def test_missing_origin_trunk_is_a_quiet_noop(parked, caplog):
    parked.write_board()
    _git(parked.clone, "update-ref", "-d", "refs/remotes/origin/main")
    _git(parked.clone, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    before = parked.head_state()

    with caplog.at_level(logging.WARNING, logger=plugin.logger.name):
        verdict = _run(parked)

    assert verdict["action"] == "noop"
    assert verdict["reason"] == "refs/remotes/origin/main does not exist in target repo"
    assert not _has_branch(parked.clone, BRANCH)
    assert parked.head_state() == before
    assert any("has no refs/remotes/origin/main" in r.getMessage() for r in caplog.records)


# (d) NEGATIVE CONTROL --------------------------------------------------------

def test_kill_switch_disables_it(parked):
    parked.write_board()
    verdict = _run(parked, env={plugin.ENV_BASE_CURRENT: "0"})
    assert verdict["action"] == "noop" and verdict["reason"] == "disabled by env"
    assert not _has_branch(parked.clone, BRANCH)
    # ...and the carry-over kill switch does NOT reach this phase.
    verdict = _run(parked, env={plugin.ENV_KILL_SWITCH: "0"})
    assert verdict["action"] == "based"


# (e) -----------------------------------------------------------------------

def test_tenant_trunk_override_is_respected(parked):
    _git(parked.origin, "checkout", "-b", "develop")
    dev_head = _commit(parked.origin, "dev.txt", "feat: on develop")
    _git(parked.clone, "fetch", "origin")
    assert _rev(parked.clone, "refs/remotes/origin/develop") == dev_head
    parked.write_tenants(trunk="develop")
    parked.write_board()

    verdict = _run(parked)

    assert verdict["action"] == "based", verdict
    assert verdict["tenant"] == "weroll-app"
    assert (verdict["trunk"], verdict["trunk_source"]) == ("develop", "tenant")
    assert _rev(parked.clone, f"refs/heads/{BRANCH}") == dev_head


def test_tenant_trunk_accepts_ref_spellings(parked):
    for spelling in ("origin/main", "refs/remotes/origin/main", "refs/heads/main"):
        assert plugin._resolve_trunk(parked.clone, {"trunk": spelling}) == ("main", "tenant")


def test_tenant_without_trunk_falls_back_to_origin_head(parked):
    parked.write_tenants()
    parked.write_board()
    verdict = _run(parked)
    assert verdict["tenant"] == "weroll-app"
    assert (verdict["trunk"], verdict["trunk_source"]) == ("main", "origin/HEAD")


def test_no_origin_head_defaults_to_main(parked):
    _git(parked.clone, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    parked.write_board()
    verdict = _run(parked)
    assert verdict["action"] == "based"
    assert (verdict["trunk"], verdict["trunk_source"]) == ("main", "default")


# staleness / narrowness -------------------------------------------------------

def test_stale_fetch_head_is_only_a_warning(parked, caplog):
    parked.write_board()
    _git(parked.clone, "fetch", "origin")
    fetch_head = parked.clone / ".git" / "FETCH_HEAD"
    old = time.time() - 3 * 3600
    os.utime(fetch_head, (old, old))

    with caplog.at_level(logging.WARNING, logger=plugin.logger.name):
        verdict = _run(parked)                      # default limit 30 min
    assert verdict["action"] == "based"
    assert verdict["fetch_head_age_min"] > 170
    assert any(str(parked.clone) in r.getMessage() and "last fetched" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)

    caplog.clear()
    _git(parked.clone, "branch", "-D", BRANCH)
    with caplog.at_level(logging.WARNING, logger=plugin.logger.name):
        _run(parked, env={plugin.ENV_BASE_CURRENT_MAX_AGE: "600"})
    assert not any("last fetched" in r.getMessage() for r in caplog.records)


def test_card_without_branch_name_is_ignored(parked):
    parked.write_board(branch_name=None)
    verdict = _run(parked)
    assert verdict["action"] == "noop" and verdict["reason"] == "card has no branch_name"
    assert not _has_branch(parked.clone, f"wt/{TASK}")


def test_non_worktree_card_is_ignored(parked):
    parked.write_board(workspace_kind="shared")
    verdict = _run(parked)
    assert verdict["action"] == "noop" and verdict["reason"] == "not a worktree card"
    assert not _has_branch(parked.clone, BRANCH)


def test_unknown_card_is_a_noop(parked):
    verdict = plugin.base_current("t_nope", "default", db_path=parked.db, env={})
    assert verdict["action"] == "noop"


def test_hook_runs_base_current_after_carry_over(parked, monkeypatch):
    """The dispatcher passes only ``task_id``/``board``: the hook must discover
    the board and run BOTH phases, carry-over first, then base-current."""
    parked.write_board()
    order = []
    real_carry, real_base = plugin.carry_over, plugin.base_current
    monkeypatch.setattr(plugin, "carry_over", lambda *a, **k: (order.append("carry"), real_carry(*a, **k))[1])
    monkeypatch.setattr(plugin, "base_current", lambda *a, **k: (order.append("base"), real_base(*a, **k))[1])

    plugin.on_task_claimed(task_id=TASK, board="default", assignee="bob", run_id=1, profile_name="bob")

    assert order == ["carry", "base"]
    assert _rev(parked.clone, f"refs/heads/{BRANCH}") == parked.trunk_head


def test_hook_never_raises_even_when_base_current_explodes(parked, monkeypatch):
    parked.write_board()

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "base_current", boom)
    plugin.on_task_claimed(task_id=TASK, board="default")     # must not raise


def test_manifest_bumped():
    import yaml

    manifest = yaml.safe_load((_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.0"
    assert "HERMES_KANBAN_BASE_CURRENT" in manifest["description"]
    assert manifest["provides_hooks"] == ["kanban_task_claimed"]
