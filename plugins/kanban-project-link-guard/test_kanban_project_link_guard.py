"""Calibration set for the project-link guard (card t_331cb549, reported by jobsy 2026-09-16).

Two halves, and the second is the one that matters:

  * the REFUSAL cases are the day's real evidence — the two cards that were wasted
    (`t_ca4b119d`, `t_135274de`) because an explicit `project` that did not resolve was
    dropped silently and a repo-less scratch card was minted instead;
  * the ALLOW cases are the no-regression set, exercised against a REAL ``projects.db``
    and the REAL ``kanban_db.create_task``: a guard that refuses a card the kernel would
    have linked correctly stops the board, which is worse than the defect it prevents.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_WORKTREE = pathlib.Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

_spec = importlib.util.spec_from_file_location(
    "project_link_guard", pathlib.Path(__file__).with_name("__init__.py"))
plg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plg)

from hermes_cli import kanban_db as kb            # noqa: E402
from hermes_cli import kanban_db_connect as kbc   # noqa: E402
from hermes_cli import projects_db as pdb         # noqa: E402


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME, so projects.db and kanban.db are the test's own."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_BOARD",
                # A worker's own delegated-child flag makes every board mutation fail closed
                # (kanban_db._assert_not_delegated_child_mutation). These tests exercise the
                # kernel directly, so they must not inherit the harness they run under.
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    for mod in (kb, pdb):
        getattr(mod, "_INITIALIZED_PATHS", {}).clear()
    return home


@pytest.fixture
def store(fresh_home, tmp_path):
    """A real project in a real (temp) projects.db — the store the guard reads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name="BackupBrain", primary_path=str(repo))
        obj = pdb.get_project(conn, pid)
    return {"id": pid, "slug": obj.slug, "repo": repo}


# --- AC 1: the evidence, as calls -------------------------------------------

# The exact two calls that produced a silent scratch card on 2026-09-16.
WASTED = [
    {"project": "meeting-notes-transcription"},   # a slug that is not in the store
    {"project": "p_3d4a6fe1"},                    # an id that lives on the board, not here
]


@pytest.mark.parametrize("args", WASTED, ids=["slug-that-does-not-exist", "id-from-another-registry"])
def test_the_two_wasted_cards_are_now_refused(store, args):
    message = plg.verdict(args)
    assert message, f"{args} must be refused, not silently minted as a scratch card"
    assert args["project"] in message
    assert "Refusing to mint this card" in message
    # The refusal has to teach, not just refuse: a typo is only self-correcting if the
    # caller is shown what the store actually holds.
    assert store["id"] in message and store["slug"] in message


def test_the_refusal_names_the_key_presence_semantics(store):
    """The three ways out must be in the message, or a refused caller guesses."""
    message = plg.verdict({"project": "nope"})
    assert "hermes project list" in message
    assert 'OMIT `project`' in message
    assert 'project=""' in message


def test_the_hook_refuses_the_real_call(store):
    result = plg.on_pre_tool_call(tool_name="kanban_create", args={"project": "p_3d4a6fe1"})
    assert isinstance(result, dict) and result["action"] == "block"
    assert "p_3d4a6fe1" in result["message"]


# --- AC 2 + 3: no regression, against the real store and the real kernel -----

def test_a_resolvable_project_is_allowed_and_still_binds_a_worktree(store):
    """AC 2: the guard allows it AND the kernel does what it always did."""
    args = {"project": store["id"]}
    assert plg.verdict(args) is None

    kb.create_board("scoped", name="Scoped", project_id=store["id"])
    conn = kbc.connect(board="scoped")
    try:
        task = kb.get_task(conn, kb.create_task(
            conn, title="bind me", board="scoped", project_id=store["id"]))
        assert (task.workspace_kind, task.project_id) == ("worktree", store["id"])
        assert task.workspace_path.endswith(f".worktrees/{task.id}")
    finally:
        conn.close()


def test_a_slug_is_allowed_too(store):
    """The store's slug is a legitimate value — the guard must not be id-only."""
    assert plg.verdict({"project": store["slug"]}) is None


def test_no_project_still_inherits_the_board_project(store):
    """AC 3: the implicit path is untouched, and the kernel still inherits."""
    assert plg.verdict({}) is None
    assert plg.verdict({"workspace_kind": "worktree"}) is None

    kb.create_board("scoped2", name="Scoped2", project_id=store["id"])
    conn = kbc.connect(board="scoped2")
    try:
        task = kb.get_task(conn, kb.create_task(
            conn, title="inherit me", board="scoped2", workspace_kind="worktree"))
        assert (task.workspace_kind, task.project_id) == ("worktree", store["id"])
    finally:
        conn.close()


# --- the shapes that must NEVER be refused -----------------------------------

def test_explicit_no_project_is_allowed(store):
    """``project=""`` is a deliberate scratch card (#67567 / #106342) — not a typo."""
    assert plg.explicit_project({"project": ""}) is None
    assert plg.verdict({"project": ""}) is None


def test_project_none_is_the_implicit_path(store):
    """``project=None`` still takes the inherit path, so the guard must not fire."""
    assert plg.verdict({"project": None}) is None


def test_project_id_alias_is_read(store):
    assert plg.explicit_project({"project_id": "p_3d4a6fe1"}) == "p_3d4a6fe1"
    assert plg.verdict({"project_id": "p_3d4a6fe1"})


def test_a_non_string_project_is_stringified_not_crashed(store):
    assert plg.explicit_project({"project": 12345}) == "12345"
    assert plg.verdict({"project": 12345})       # resolves nothing -> refused, no crash


def test_the_project_key_wins_over_the_alias(store):
    """Mirrors ``kanban_tools`` line 887 exactly."""
    assert plg.explicit_project({"project": "a", "project_id": "b"}) == "a"


def test_other_tools_are_untouched(store):
    assert plg.on_pre_tool_call(tool_name="kanban_link",
                                args={"project": "p_3d4a6fe1"}) is None
    assert plg.on_pre_tool_call(tool_name="kanban_update",
                                args={"project": "p_3d4a6fe1"}) is None


# --- fail-open, in every direction -------------------------------------------

def test_hook_fails_open_on_a_broken_payload(store):
    assert plg.on_pre_tool_call() is None
    assert plg.on_pre_tool_call(tool_name="kanban_create") is None
    assert plg.on_pre_tool_call(tool_name="kanban_create", args="not-a-dict") is None
    assert plg.verdict("not-a-dict") is None


def test_an_unreadable_store_allows(store, monkeypatch):
    """A broken store is not a wrong project. Allow — a guard must never be a crash surface."""
    def boom(*a, **k):
        raise RuntimeError("projects.db is unreadable")
    monkeypatch.setattr(pdb, "connect_closing", boom)
    assert plg.resolves("p_3d4a6fe1") is True
    assert plg.verdict({"project": "p_3d4a6fe1"}) is None


def test_an_empty_store_refuses(fresh_home):
    """A worker whose projects.db is EMPTY is the common case, not an edge case.

    ``projects_db.connect`` initialises the schema on demand, so "no store" is "no
    projects", which is a definitive answer — the kernel would drop the link, so the
    guard must refuse rather than let the silent scratch card through.
    """
    assert plg.resolves("anything") is False
    assert plg.verdict({"project": "anything"})


# --- negative controls: the suite can go red ---------------------------------

def test_negative_control_the_refusal_depends_on_resolution(store, monkeypatch):
    """With resolution forced true, the refusal disappears — so the assertions above
    are testing the rule and not an always-truthy message."""
    monkeypatch.setattr(plg, "resolves", lambda value: True)
    assert plg.verdict({"project": "p_3d4a6fe1"}) is None


def test_negative_control_the_implicit_path_is_reachable(store):
    """If the guard fired on everything, ``verdict({})`` would refuse — it does not."""
    assert plg.verdict({}) is None
    assert plg.explicit_project({}) is None
