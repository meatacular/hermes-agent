"""Acceptance tests for the worktree carry-over plugin (kanban t_b7585516).

The defect: a worktree card whose resolved repo CHANGES between runs is handed
a branch cut fresh from HEAD, so the retry worker cannot see the implementation
its reviewer already read. Reproduced here against the REAL kernel function
(``kanban_db_workspace._resolve_worktree_workspace``), not a mock: two clones of
one origin, the card anchored in clone A, its ``workspace_path`` then cleared so
dispatch resolves clone B.

AC1  a retry keeps the prior run's commits on its branch
AC2  the worker can ``git log`` them and continue
AC3  a first provision still creates from base (regression)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PLUGIN_DIR.parents[1]

BRANCH = "proj/t_retry0001-weekly-digest"
TASK = "t_retry0001"
HOOK = "kanban_task_claimed"


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "kanban_worktree_carryover_under_test", _PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


# --------------------------------------------------------------------------
# git / board fixtures
# --------------------------------------------------------------------------

def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "kanban@example.com")
    _git(path, "config", "user.name", "Kanban Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def _commit(path: Path, filename: str, body: str, message: str) -> str:
    (path / filename).write_text(body, encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


class Incident:
    """One card, two clones of one origin, prior work in the first clone."""

    def __init__(self, tmp_path: Path):
        self.origin = tmp_path / "origin"
        self.prior = tmp_path / "prior clone"          # the stale clone
        self.target = tmp_path / "target clone"        # the canonical clone
        self.db = tmp_path / "kanban.db"

        _init_repo(self.origin)
        _git(tmp_path, "clone", str(self.origin), str(self.prior))
        _git(tmp_path, "clone", str(self.origin), str(self.target))
        for repo in (self.prior, self.target):
            _git(repo, "config", "user.email", "kanban@example.com")
            _git(repo, "config", "user.name", "Kanban Test")

        self.base = _git(self.target, "rev-parse", "HEAD").stdout.strip()

        # The prior run: branch cut from base, then the implementation.
        self.prior_ws = self.prior / ".worktrees" / TASK
        self.target_ws = self.target / ".worktrees" / TASK
        _git(self.prior, "worktree", "add", "-b", BRANCH, str(self.prior_ws), "HEAD")
        _commit(self.prior_ws, "digest.py", "def weekly():\n    return []\n",
                "feat: add weekly retrospective digest")
        self.prior_head = _commit(self.prior_ws, "test_digest.py", "def test_weekly():\n    assert True\n",
                                  "test: fix weekly digest import path")

    # -- board -------------------------------------------------------------
    def write_board(self, runs: int = 2, anchor: bool = True,
                    workspace_path: str | None = None) -> None:
        from hermes_cli import kanban_db_connect as kbc

        kbc.init_db(db_path=self.db)
        import sqlite3

        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, workspace_kind, "
                " workspace_path, branch_name, tenant, created_at, created_by, max_cost, "
                " block_kind, project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (TASK, "[Bob] WP7-B weekly digest", "", "bob", "running", "worktree",
                 workspace_path or str(self.target_ws), BRANCH, None, int(time.time()),
                 "jobsy", 1.0, None, "p_5fe7127d"),
            )
            for i in range(runs):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                    "VALUES (?,?,?,?,?,?)",
                    (TASK, "bob", "done", int(time.time()) - 100 + i, int(time.time()) - 50 + i,
                     "review_requested" if i == 0 else "changes_requested"),
                )
            if anchor:
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                    (TASK, "created",
                     json.dumps({"assignee": "bob", "workspace_kind": "worktree",
                                 "workspace_path": str(self.prior / ".worktrees" / "t_root0001")}),
                     int(time.time()) - 200),
                )
            conn.commit()
        finally:
            conn.close()

    def task(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing(self.db) as conn:
            return kb.get_task(conn, TASK)

    def provision(self, task_id: str = TASK):
        """Run the REAL kernel provisioning against the card's target repo."""
        from hermes_cli import kanban_db_workspace as kbw

        task = self.task()
        assert task is not None
        return kbw._resolve_worktree_workspace(task)

    def log_subjects(self, worktree: Path) -> list:
        out = _git(worktree, "log", "--format=%s").stdout.strip()
        return [line for line in out.splitlines() if line]


@pytest.fixture
def incident(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv(plugin.ENV_KILL_SWITCH, raising=False)
    return Incident(tmp_path)


# --------------------------------------------------------------------------
# the defect, before the fix — proves the reproduction is faithful
# --------------------------------------------------------------------------

def test_without_carryover_the_retry_is_cut_from_base(incident):
    incident.write_board()
    worktree, branch = incident.provision()

    assert Path(worktree) == incident.target_ws
    assert branch == BRANCH
    # The symptom Bob reported: the retry's branch holds nothing but the base.
    assert incident.log_subjects(incident.target_ws) == ["init"]
    assert _git(incident.target_ws, "rev-parse", "HEAD").stdout.strip() == incident.base
    assert not (incident.target_ws / "digest.py").exists()
    # ...while the prior work still exists, untouched, in the other clone.
    assert _git(incident.prior, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == incident.prior_head


def test_plan_alone_changes_nothing(incident):
    incident.write_board()
    verdict = plugin.plan(TASK, "default", db_path=incident.db, env={})

    assert verdict["action"] == "carry"
    assert verdict["branch"] == BRANCH
    assert Path(verdict["target_repo"]) == incident.target
    # Read-only: the branch must still be absent in the target repo.
    assert _git(incident.target, "show-ref", "--verify", f"refs/heads/{BRANCH}",
                check=False).returncode != 0


# --------------------------------------------------------------------------
# AC1 + AC2
# --------------------------------------------------------------------------

def test_retry_worktree_keeps_the_prior_commits(incident):
    incident.write_board()

    verdict = plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "carried", verdict
    assert verdict["source"] == str(incident.prior / ".worktrees" / "t_root0001")
    assert verdict["head"] == incident.prior_head

    worktree, branch = incident.provision()
    assert branch == BRANCH

    subjects = incident.log_subjects(incident.target_ws)
    assert "feat: add weekly retrospective digest" in subjects
    assert "test: fix weekly digest import path" in subjects
    assert subjects[0] == "test: fix weekly digest import path"
    # AC2: the worker can read the implementation, not just the log.
    assert (incident.target_ws / "digest.py").read_text(encoding="utf-8") == "def weekly():\n    return []\n"
    assert (incident.target_ws / "test_digest.py").exists()
    assert _git(incident.target_ws, "rev-parse", "HEAD").stdout.strip() == incident.prior_head


def test_carryover_is_recorded_on_the_board(incident):
    incident.write_board()
    plugin.carry_over(TASK, "default", db_path=incident.db, env={})

    import sqlite3

    conn = sqlite3.connect(incident.db)
    try:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'worktree_carryover'",
            (TASK,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["branch"] == BRANCH
    assert payload["head"] == incident.prior_head


def test_carried_branch_is_reused_on_the_next_retry(incident):
    """Second retry: the branch is now local, so the kernel reuses it and the
    plugin stands down — the fix is idempotent, not a repeated repair."""
    incident.write_board()
    plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    incident.provision()

    again = plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert again["action"] == "noop"
    assert again["reason"] == "branch already present in target repo"


def test_origin_is_used_when_the_prior_clone_is_gone(incident):
    """The clone the card was first built in can be deleted. Pushed work is
    then recoverable from the remote-tracking ref the target repo already holds."""
    incident.write_board()
    _git(incident.prior, "push", "origin", BRANCH)
    _git(incident.target, "fetch", "origin")
    # Drop the local anchor: only the remote copy of the branch survives.
    import shutil

    shutil.rmtree(incident.prior)

    verdict = plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "carried", verdict
    assert verdict["source"] == "remote:origin"

    incident.provision()
    assert "feat: add weekly retrospective digest" in incident.log_subjects(incident.target_ws)


# --------------------------------------------------------------------------
# AC3 — regression: first provision is untouched
# --------------------------------------------------------------------------

def test_first_provision_still_creates_from_base(incident):
    incident.write_board(runs=1, anchor=False)

    verdict = plugin.plan(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "first provision"

    plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert _git(incident.target, "show-ref", "--verify", f"refs/heads/{BRANCH}",
                check=False).returncode != 0

    worktree, branch = incident.provision()
    assert branch == BRANCH
    assert incident.log_subjects(incident.target_ws) == ["init"]
    assert _git(incident.target_ws, "rev-parse", "HEAD").stdout.strip() == incident.base


def test_no_earlier_copy_anywhere_is_a_quiet_noop(incident):
    """A retry of a card that never got as far as a branch: nothing to carry,
    nothing fetched, no branch invented."""
    incident.write_board(anchor=False)

    verdict = plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "branch absent and no earlier copy found"
    assert _git(incident.target, "show-ref", "--verify", f"refs/heads/{BRANCH}",
                check=False).returncode != 0


# --------------------------------------------------------------------------
# controls and fail-open behaviour
# --------------------------------------------------------------------------

def test_kill_switch_disables_it(incident):
    incident.write_board()
    verdict = plugin.carry_over(TASK, "default", db_path=incident.db,
                                env={plugin.ENV_KILL_SWITCH: "0"})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "disabled by env"
    assert _git(incident.target, "show-ref", "--verify", f"refs/heads/{BRANCH}",
                check=False).returncode != 0


@pytest.mark.parametrize("task_id,board,db_path", [
    ("t_does_not_exist", "default", None),
    ("", "default", None),
])
def test_unknown_cards_are_a_noop(tmp_path, monkeypatch, task_id, board, db_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    verdict = plugin.carry_over(task_id, board, db_path=db_path, env={})
    assert verdict["action"] == "noop"


def test_non_worktree_cards_are_ignored(incident):
    incident.write_board()
    import sqlite3

    conn = sqlite3.connect(incident.db)
    try:
        conn.execute("UPDATE tasks SET workspace_kind = 'scratch' WHERE id = ?", (TASK,))
        conn.commit()
    finally:
        conn.close()

    verdict = plugin.plan(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "not a worktree card"


def test_hook_is_registered_and_never_raises(incident):
    registered = {}

    class Ctx:
        def register_hook(self, name, cb):
            registered[name] = cb

    plugin.register(Ctx())
    assert set(registered) == {"kanban_task_claimed"}

    # The handler must swallow anything, including a malformed payload.
    registered["kanban_task_claimed"](task_id=None, board=None)
    registered["kanban_task_claimed"](task_id="t_nope", board="no-such-board")
    registered["kanban_task_claimed"]()


def test_hook_end_to_end_through_env_discovery(incident):
    """The dispatcher passes no db path — the hook must find the board itself."""
    incident.write_board()
    plugin.on_task_claimed(task_id=TASK, board="default")
    assert _git(incident.target, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == incident.prior_head


_PROBE = '''
import json, sys
from hermes_cli import plugins as P
from hermes_cli import lifecycle

P.discover_plugins(force=True)
assert P.has_hook("kanban_task_claimed"), "hook not registered through real discovery"
lifecycle.invoke_hook("kanban_task_claimed", task_id=sys.argv[1], board="default",
                      assignee="bob", run_id=2, profile_name="default")
print("PROBE-OK")
'''


def test_hook_end_to_end_through_real_plugin_discovery(incident, tmp_path):
    """The whole chain, in a fresh interpreter: bundled-plugin discovery gated by
    ``plugins.enabled`` -> ``register`` -> ``lifecycle.invoke_hook`` -> carry-over.
    Loaded by file path the plugin proves nothing; this is the path the gateway uses."""
    incident.write_board()

    home = tmp_path / "probe-home"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - kanban-worktree-carryover\n", encoding="utf-8"
    )
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE, encoding="utf-8")

    env = dict(os.environ)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_DB": str(incident.db),
        "PYTHONPATH": str(_REPO_ROOT),
    })
    import sys

    proc = subprocess.run([sys.executable, str(probe), TASK], env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PROBE-OK" in proc.stdout

    assert _git(incident.target, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == incident.prior_head
    # and the kernel then hands the worker that work
    incident.provision()
    assert "feat: add weekly retrospective digest" in incident.log_subjects(incident.target_ws)


def test_bundled_backend_autoloads_without_a_config_entry(incident, tmp_path):
    """The deploy gate for this plugin is the MERGE + gateway restart, not a
    config entry: ``plugins_discovery`` auto-loads a bundled ``kind: backend``
    (plugins_discovery.py:205-208). ``kind: backend`` in plugin.yaml is therefore
    load-bearing — flip it to ``standalone`` and the plugin silently stops
    running unless every dispatcher profile names it in ``plugins.enabled``,
    which is exactly how the completion gate was a no-op from 09-09 to 09-11."""
    incident.write_board()

    home = tmp_path / "probe-home-autoload"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - some-other-plugin\n", encoding="utf-8"
    )
    probe = tmp_path / "probe_autoload.py"
    probe.write_text(_PROBE, encoding="utf-8")

    env = dict(os.environ)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_DB": str(incident.db),
        "PYTHONPATH": str(_REPO_ROOT),
    })
    import sys

    proc = subprocess.run([sys.executable, str(probe), TASK], env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PROBE-OK" in proc.stdout
    assert _git(incident.target, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == incident.prior_head


_IMPORT_PROBE = '''
import importlib.util, sys
before = set(sys.modules)
spec = importlib.util.spec_from_file_location("carryover_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
new = sorted(m for m in set(sys.modules) - before if m.split(".")[0] == "hermes_cli")
print("IMPORTED:" + ",".join(new))
'''


def test_plugin_has_zero_merge_surface(tmp_path):
    """It must not import from hermes_cli: upstream moving a symbol inside its
    own package can then never break this plugin (plugin.yaml's contract)."""
    import sys

    probe = tmp_path / "import_probe.py"
    probe.write_text(_IMPORT_PROBE, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(probe), str(_PLUGIN_DIR / "__init__.py")],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "IMPORTED:" in proc.stdout
    imported = proc.stdout.split("IMPORTED:", 1)[1].strip()
    assert imported == "", f"plugin pulled in {imported}"


def test_manifest_is_a_bundled_backend():
    """Load-bearing manifest fields, asserted rather than assumed."""
    import yaml

    manifest = yaml.safe_load((_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "kanban-worktree-carryover"
    assert manifest["kind"] == "backend"      # auto-loads; see the test above
    assert manifest["provides_hooks"] == [HOOK]
