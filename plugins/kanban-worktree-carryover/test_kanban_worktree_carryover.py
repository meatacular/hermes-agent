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

Naming (charter §7) — the mapping is readable from collection:

    plugin AC1 -> test_ac1_retry_keeps_the_prior_commits_on_its_branch
    plugin AC2 -> test_ac2_worker_can_read_and_continue_the_carried_work
    plugin AC3 -> test_ac3_first_provision_still_creates_from_base

Follow-up card t_2a901db6:

    AC1 -> test_profile_home_resolves_to_the_shared_board
           (+ test_profile_home_resolution_mirrors_the_kernel)
    AC2 -> test_remote_candidate_never_touches_the_network
    AC3 -> test_cleared_anchor_falls_back_to_board_default_workdir
    AC4 -> this file, run as:
           env -u HERMES_DELEGATED_CHILD_CONTEXT <venv>/bin/python -m pytest \
               plugins/kanban-worktree-carryover/test_kanban_worktree_carryover.py \
               -q -p no:randomly
    AC5 -> test_root_home_path_is_unchanged (self-contained twin of the live
           calibration: t_17ee37d5 -> "branch already present in target repo",
           first provisions -> "first provision")
    AC6 -> test_plugin_has_zero_merge_surface + test_manifest_is_a_bundled_backend
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PLUGIN_DIR.parents[1]

BRANCH = "proj/t_retry0001-weekly-digest"
TASK = "t_retry0001"
HOOK = "kanban_task_claimed"

#: write_board()'s default anchor is the target clone; pass None for SQL NULL.
_DEFAULT_WS = object()


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
                    workspace_path=_DEFAULT_WS) -> None:
        """Seed the board. ``workspace_path=None`` writes SQL NULL — the shape the
        09-14 incident actually had (the path was cleared mid retry-cycle so the
        board anchor fell back to the board's ``default_workdir``). Omit it for
        the ordinary shape, where the card points at the target clone."""
        from hermes_cli import kanban_db_connect as kbc

        kbc.init_db(db_path=self.db)
        import sqlite3

        ws_value = str(self.target_ws) if workspace_path is _DEFAULT_WS else workspace_path
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, workspace_kind, "
                " workspace_path, branch_name, tenant, created_at, created_by, max_cost, "
                " block_kind, project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (TASK, "[Bob] WP7-B weekly digest", "", "bob", "running", "worktree",
                 ws_value, BRANCH, None, int(time.time()),
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

    def write_board_default_workdir(self, workdir) -> None:
        """Set the board's ``default_workdir``, at the KERNEL's path (so the plugin
        is never confirmed by its own idea of where board.json lives)."""
        from hermes_cli import kanban_db as kb

        meta = kb.board_metadata_path("default")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"slug": "default", "default_workdir": str(workdir)}),
                        encoding="utf-8")

    def add_event(self, kind: str, payload: dict) -> None:
        """Append a board event (e.g. an operator's path change)."""
        import sqlite3

        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                (TASK, kind, json.dumps(payload), int(time.time()) - 100),
            )
            conn.commit()
        finally:
            conn.close()

    def del_runs(self, n: int = 1) -> None:
        """Drop the newest ``n`` runs, to re-create a first-provision shape."""
        import sqlite3

        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "DELETE FROM task_runs WHERE rowid IN (SELECT rowid FROM task_runs "
                "WHERE task_id = ? ORDER BY started_at DESC LIMIT ?)",
                (TASK, int(n)),
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

def test_ac1_retry_keeps_the_prior_commits_on_its_branch(incident):
    """Plugin AC1 — a retry keeps the prior run's commits on its branch."""
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
    assert _git(incident.target_ws, "rev-parse", "HEAD").stdout.strip() == incident.prior_head


def test_ac2_worker_can_read_and_continue_the_carried_work(incident):
    """Plugin AC2 — the worker can ``git log`` them, READ the implementation and
    continue on top of it (not merely see a history it cannot use)."""
    incident.write_board()
    plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    incident.provision()

    # read it: the files are on disk, not only in the log
    assert (incident.target_ws / "digest.py").read_text(encoding="utf-8") == "def weekly():\n    return []\n"
    assert (incident.target_ws / "test_digest.py").exists()

    # continue it: a new commit lands ON TOP of the carried head
    head = _commit(incident.target_ws, "digest_extra.py", "VALUE = 1\n",
                   "feat: continue the weekly digest")
    subjects = incident.log_subjects(incident.target_ws)
    assert subjects[0] == "feat: continue the weekly digest"
    assert subjects[-1] == "init"
    assert head != incident.prior_head
    assert _git(incident.target_ws, "rev-parse", "HEAD~1").stdout.strip() == incident.prior_head


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

def test_ac3_first_provision_still_creates_from_base(incident):
    """Plugin AC3 — regression: a first provision is untouched."""
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


# --------------------------------------------------------------------------
# t_2a901db6 AC1 — the guard must not be inert in a profile-owned dispatcher
# --------------------------------------------------------------------------

@pytest.mark.parametrize("shape", ["root-home", "profile-home", "custom-root",
                                   "custom-profile-root"])
def test_profile_home_resolution_mirrors_the_kernel(tmp_path, monkeypatch, shape):
    """The mirror is only worth having if it AGREES with the kernel it mirrors.

    ``kanban_home()`` is ``HERMES_KANBAN_HOME`` else ``get_default_hermes_root()``,
    so the plugin must never invent a second opinion; it may not import
    ``hermes_cli``, so the reference is compared here instead."""
    native = Path.home() / ".hermes"
    if shape == "root-home":
        home = tmp_path / "root-home"
    elif shape == "profile-home":
        home = native / "profiles" / "brain"          # under the native home
    elif shape == "custom-root":
        home = tmp_path / "docker" / "hermes"         # custom root, no profile segment
    else:
        home = tmp_path / "docker" / "profiles" / "other"
    home.mkdir(parents=True, exist_ok=True)

    from hermes_cli import kanban_db as kb

    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert plugin._kanban_home() == Path(kb.kanban_home()), shape

    # HERMES_KANBAN_HOME still wins outright, exactly as it does in the kernel.
    pin = tmp_path / "pinned-kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(pin))
    assert plugin._kanban_home() == Path(kb.kanban_home()) == pin


def test_profile_home_resolves_to_the_shared_board(incident, tmp_path, monkeypatch):
    """Card AC1 — a profile-owned dispatcher must still FIND the board.

    ``HERMES_HOME=<root>/profiles/<name>`` is how a profile gateway runs, and the
    board is shared across profiles by design: the kernel resolves back to
    ``<root>``. Returning ``HERMES_HOME`` verbatim made this plugin silently
    inert in exactly those dispatchers — ``brain``, ``switch`` and ``axel`` have
    each held the machine-global dispatcher lease, and non-root ownership is
    supported on purpose (``_should_seize_dispatcher``)."""
    root = tmp_path / "hermes-root"
    (root / "profiles" / "brain").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "brain"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)

    incident.db = root / "kanban.db"          # where the kernel keeps the default board
    incident.write_board()

    from hermes_cli import kanban_db as kb

    assert Path(kb.kanban_db_path("default")) == root / "kanban.db"   # the reference
    assert plugin._kanban_home() == root                              # the mirror
    assert plugin._find_db(TASK, "default") == root / "kanban.db"

    verdict = plugin.plan(TASK, "default", env={})     # no db_path: it must discover
    assert verdict["reason"] != "no board db holds this task"
    assert verdict["action"] == "carry", verdict
    assert Path(verdict["target_repo"]) == incident.target

    carried = plugin.carry_over(TASK, "default", env={})
    assert carried["action"] == "carried", carried
    assert _git(incident.target, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == incident.prior_head


# --------------------------------------------------------------------------
# t_2a901db6 AC2 — the remote candidate must not go to the network
# --------------------------------------------------------------------------

def test_remote_candidate_never_touches_the_network(incident, tmp_path, monkeypatch):
    """Card AC2 — the ``remote:<name>`` path branches from the local tracking ref.

    ``plan()`` only offers ``remote:<name>`` when ``refs/remotes/<remote>/<branch>``
    is already local, so the objects are in the target repo already. Fetching
    first is pure latency on the one path that must stay fast: the hook runs
    inline under the machine-global dispatch lock (``kanban_db_dispatch``), and
    MAX_SOURCES x GIT_TIMEOUT was up to 80 s of stall on a dispatch tick."""
    incident.write_board()
    _git(incident.prior, "push", "origin", BRANCH)
    _git(incident.target, "fetch", "origin")
    shutil.rmtree(incident.prior)               # only the remote copy survives
    # Dead remote: any fetch of it must fail. A test that passes by fetching it
    # would be a test of the network, not of the plugin.
    _git(incident.target, "remote", "set-url", "origin", str(tmp_path / "dead-remote"))

    fetches = []
    real_git = plugin._git

    def spy(cwd, *args, timeout=plugin.GIT_TIMEOUT):
        if args and args[0] == "fetch":
            fetches.append((str(cwd), tuple(args)))
        return real_git(cwd, *args, timeout=timeout)

    monkeypatch.setattr(plugin, "_git", spy)

    verdict = plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert fetches == [], f"the remote path went to the network: {fetches}"
    assert verdict["action"] == "carried", verdict
    assert verdict["source"] == "remote:origin"
    assert verdict["head"] == incident.prior_head

    incident.provision()
    assert (incident.target_ws / "digest.py").exists()
    assert "feat: add weekly retrospective digest" in incident.log_subjects(incident.target_ws)


# --------------------------------------------------------------------------
# t_2a901db6 AC3 — the incident's OWN shape: workspace_path NULL + board default
# --------------------------------------------------------------------------

def test_cleared_anchor_falls_back_to_board_default_workdir(incident):
    """Card AC3 — the card's ``workspace_path`` was CLEARED (the standard "point
    it back at the canonical clone" remedy) and the board's ``default_workdir``
    is what dispatch then resolves. No test reached that shape: the shipped one
    returns at ``runs < 2`` before the anchor is even read."""
    incident.write_board(workspace_path=None)                 # SQL NULL, not the target
    incident.write_board_default_workdir(incident.target)

    from hermes_cli import kanban_db as kb

    # The anchor dispatch will use is the board's, and the kernel agrees.
    assert kb.read_board_metadata("default")["default_workdir"] == str(incident.target)

    verdict = plugin.plan(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "carry", verdict
    assert Path(verdict["target_repo"]) == incident.target
    assert verdict["candidates"][0]["source"] == str(incident.prior / ".worktrees" / "t_root0001")

    carried = plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert carried["action"] == "carried", carried
    assert carried["head"] == incident.prior_head

    # The kernel then materialises the worktree from the board default, and the
    # prior commits and files arrive with it.
    worktree, branch = incident.provision()
    assert Path(worktree) == incident.target_ws
    assert branch == BRANCH
    assert incident.log_subjects(incident.target_ws)[0] == "test: fix weekly digest import path"
    assert (incident.target_ws / "digest.py").read_text(encoding="utf-8") == "def weekly():\n    return []\n"
    assert _git(incident.target_ws, "rev-parse", "HEAD").stdout.strip() == incident.prior_head


# --------------------------------------------------------------------------
# t_2a901db6 AC5 — the root dispatcher path is unchanged
# --------------------------------------------------------------------------

def test_root_home_path_is_unchanged(incident, tmp_path, monkeypatch):
    """Card AC5 — ``HERMES_HOME=<root>`` (the root dispatcher) still resolves to
    itself, and the two live calibrations still return the same verdicts:
    ``t_17ee37d5`` -> branch present, first provisions -> one run."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    incident.db = root / "kanban.db"
    incident.write_board()

    from hermes_cli import kanban_db as kb

    assert Path(kb.kanban_db_path("default")) == root / "kanban.db"
    assert plugin._kanban_home() == root

    # live calibration (t_17ee37d5): the branch is already in the target repo.
    _git(incident.target, "branch", BRANCH)
    verdict = plugin.plan(TASK, "default", env={})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "branch already present in target repo"

    # live calibration (first provisions): a single run, so nothing to carry.
    _git(incident.target, "branch", "-D", BRANCH)
    incident.del_runs(1)
    verdict = plugin.plan(TASK, "default", env={})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "first provision"


# --------------------------------------------------------------------------
# the remaining unreached branches
# --------------------------------------------------------------------------

def test_retry_with_no_anchor_at_all_is_a_noop(incident):
    """Neither a card path NOR a board default: fail open, quietly. Guessing a
    repo here is the failure mode the anchor check exists to prevent."""
    incident.write_board(anchor=False, workspace_path=None)

    verdict = plugin.plan(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "no anchor (no workspace_path, no board default_workdir)"


def test_anchor_outside_a_git_repo_is_a_noop(incident, tmp_path):
    stray = tmp_path / "not-a-repo" / "dir"
    stray.mkdir(parents=True)
    incident.write_board(workspace_path=str(stray))

    verdict = plugin.plan(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "noop"
    assert verdict["reason"] == "anchor is not inside a git repo"


@pytest.mark.parametrize("key", ["previous_workspace_path", "old_workspace_path"])
def test_a_recorded_previous_workspace_path_is_used_as_a_source(incident, key):
    """The board can record where a card ran BEFORE its path was re-pointed, and
    that path is where the branch is. Both keys are candidate SOURCES (newest
    first) — they are not the target anchor, so the card still needs its own
    ``workspace_path`` or a board ``default_workdir``."""
    incident.write_board(anchor=False, workspace_path=str(incident.target))
    incident.add_event("workspace_update", {key: str(incident.prior / ".worktrees" / "t_root0001")})

    verdict = plugin.carry_over(TASK, "default", db_path=incident.db, env={})
    assert verdict["action"] == "carried", verdict
    assert verdict["source"] == str(incident.prior / ".worktrees" / "t_root0001")

    incident.provision()
    assert (incident.target_ws / "digest.py").exists()
    assert "feat: add weekly retrospective digest" in incident.log_subjects(incident.target_ws)


def test_find_db_falls_back_to_named_boards_when_hook_board_is_default(tmp_path, monkeypatch):
    """boardfix-20260923: the claim hook passes board='default' for cards on other boards."""
    import sqlite3
    home = tmp_path / "hermes"; bdir = home / "kanban" / "boards" / "weroll"; bdir.mkdir(parents=True)
    (home / "kanban.db").write_bytes(b"")                      # default board: no such task
    db = bdir / "kanban.db"
    con = sqlite3.connect(db); con.execute("CREATE TABLE tasks (id TEXT)"); con.execute("INSERT INTO tasks VALUES ('t_w')"); con.commit(); con.close()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    m = _load_plugin()
    assert m._find_db("t_w", "default") == db
    assert m._find_db("t_missing", "default") is None
