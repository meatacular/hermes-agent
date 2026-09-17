"""Calibration set for the zero-delta rework guard (card t_48b49faa).

Two halves, and the second is the one that matters:

  * the REFUSAL cases are the day's real evidence — card `t_8dd715c6`, whose round-3
    handoff (run 1828) re-entered review at `4136e1e`, the exact head its round-2
    `changes_requested` had been raised at, with 0 files changed;
  * the ALLOW cases are the no-regression set, exercised against a REAL git repo and the
    REAL kanban kernel: a guard that refuses a handoff the review lane would have
    accepted stops the board, which is worse than the defect it prevents.

The hook is driven the way the runtime drives it — `on_pre_tool_call(tool_name=...,
args=...)` — with `resolve_card` (the one store read) stubbed where a test wants to
control the card, and exercised for real in `test_resolve_card_*`.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

_WORKTREE = pathlib.Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

_spec = importlib.util.spec_from_file_location(
    "review_delta_guard", pathlib.Path(__file__).with_name("__init__.py"))
plg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plg)


# --- fixtures ---------------------------------------------------------------
def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit — the worktree a card would own."""
    d = tmp_path / "worktree"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    (d / "app.py").write_text("print('one')\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "first")
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated fleet root, so a test never writes the live state dir."""
    root = tmp_path / "hermes_root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    return root


@pytest.fixture
def card(repo, monkeypatch):
    """Stub the ONE store read: the card this call belongs to."""
    info = {"id": "t_8dd715c6", "workspace": str(repo), "workspace_kind": "worktree",
            "assignee": "rodge", "run_id": 1821}
    monkeypatch.setattr(plg, "resolve_card", lambda args: dict(info))
    # The verdict-landed check (run 1821 must have landed as changes_requested on the board);
    # there is no board here, so the read is stubbed the way resolve_card is.
    monkeypatch.setattr(plg, "run_outcome", lambda run_id: "changes_requested")
    return info


def _handoff(**args):
    return plg.on_pre_tool_call(tool_name="kanban_request_review", args=args)


def _verdict(**args):
    return plg.on_pre_tool_call(tool_name="kanban_request_changes", args=args)


# --- the reported case ------------------------------------------------------
def test_the_reported_case_is_refused(repo, home, card):
    """t_8dd715c6 / run 1828: rejected at HEAD, handed back at HEAD, "present and verified"."""
    _verdict(reason="Round 2 (Execution lens) at 4136e1e — CHANGES REQUESTED")
    head = plg.git_head(str(repo))
    assert plg.read_state(card["id"])["rejected_head"] == head

    verdict = _handoff(summary="Implementation is present and verified with 5/5 focused "
                               "briefing tests, 37/37 frontend tests, 25/25 backend tests")

    assert verdict is not None and verdict["action"] == "block"
    msg = verdict["message"]
    assert head[:10] in msg                    # the sha the verdict was raised at
    assert "identical trees" in msg
    assert "kanban_request_review again" in msg
    for option in ("MOVE THE HEAD", "NAME THE BLOCKER", "DELIBERATE NO-COMMIT CARD"):
        assert option in msg


def test_zero_delta_refused_through_the_real_hook_chain(repo, tmp_path, monkeypatch):
    """The wiring, not just the decision: the kernel's own pre_tool_call dispatch, the
    real plugin module the loader builds, a real kanban DB and a real worktree card."""
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

    root = tmp_path / "chain_root"
    (root / "kanban").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(root / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    getattr(kb, "_INITIALIZED_PATHS", {}).clear()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="a worktree card", assignee="bob",
                             workspace_kind="worktree", workspace_path=str(repo))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    # The reviewer's verdict goes through the same chain and must record the head.
    verdict, modified = _dispatch_pre_tool_call_hooks(
        "kanban_request_changes", {"reason": "Round 2 at HEAD — CHANGES REQUESTED"})
    assert verdict is None and modified is None          # an observer: never blocks
    assert plg.read_state(tid)["rejected_head"] == plg.git_head(str(repo))

    block, modified = _dispatch_pre_tool_call_hooks(
        "kanban_request_review", {"summary": "Implementation is present and verified"})
    assert isinstance(block, str) and "identical trees" in block
    assert modified is None


# --- moving the head is the whole point -------------------------------------
def test_a_real_commit_is_allowed_and_carries_the_delta(repo, home, card):
    _verdict(reason="changes requested")
    (repo / "app.py").write_text("print('two')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "round 3")

    verdict = _handoff(summary="round 3")

    assert verdict is not None and verdict["action"] == "modify"
    proof = verdict["args"]["metadata"]
    assert proof["review_delta_base"] != proof["review_delta_head"]
    assert "app.py" in proof["review_delta_stat"]


def test_an_empty_commit_is_still_a_zero_delta(repo, home, card):
    """HEAD moves, the tree does not. The card's wording is 'zero delta', not 'same sha'."""
    _verdict(reason="changes requested")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "nothing but a marker")

    verdict = _handoff(summary="I committed something")

    assert verdict is not None and verdict["action"] == "block"


def test_the_caller_metadata_is_preserved_when_the_delta_is_attached(repo, home, card):
    _verdict(reason="changes requested")
    (repo / "app.py").write_text("print('three')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "round 3")

    verdict = _handoff(summary="round 3", metadata={"worker_note": "keep me"})

    assert verdict["action"] == "modify"
    assert verdict["args"]["metadata"]["worker_note"] == "keep me"
    assert "review_delta_stat" in verdict["args"]["metadata"]


# --- no-regression set ------------------------------------------------------
def test_first_review_is_untouched(repo, home, card):
    """No verdict on record -> the guard has no opinion at all."""
    assert _handoff(summary="first handoff") is None


def test_a_non_worktree_card_is_out_of_scope(monkeypatch, repo, home):
    monkeypatch.setattr(plg, "resolve_card", lambda args: None)   # resolve_card's own scope
    assert _handoff(summary="x") is None
    assert _verdict(reason="x") is None


def test_a_stray_verdict_that_never_landed_does_not_wedge_the_handoff(repo, home, card,
                                                                     monkeypatch):
    """The kernel can refuse request_changes ('not in an active review run'). A record
    whose run did not land as changes_requested must not refuse the next handoff."""
    _verdict(reason="called from the wrong lane")
    monkeypatch.setattr(plg, "run_outcome", lambda run_id: "completed")
    assert _handoff(summary="handing it back") is None


def test_an_unknown_sha_is_undecidable_and_allowed(repo, home, card):
    plg._write_state(card["id"], {"task_id": card["id"],
                                  "rejected_head": "0" * 40,
                                  "rejected_run_id": 1, "workspace": str(repo)})
    assert _handoff(summary="handoff") is None


def test_a_workspace_that_is_not_a_repo_is_allowed(tmp_path, home, card, monkeypatch):
    plg._write_state(card["id"], {"task_id": card["id"], "rejected_head": "a" * 40,
                                  "rejected_run_id": 1})
    monkeypatch.setattr(plg, "resolve_card", lambda args: {
        "id": card["id"], "workspace": str(tmp_path), "workspace_kind": "worktree",
        "assignee": "rodge", "run_id": 1821})
    assert _handoff(summary="handoff") is None


# --- the escape hatch (a bare marker must not carry it) ----------------------
@pytest.mark.parametrize("args,expected", [
    ({"metadata": {"delta-exempt": "verification-only card, no code to commit"}},
     "verification-only card, no code to commit"),
    ({"summary": "delta-exempt: audit card, deliverable is the report"}, "audit"),
    ({"metadata": {"delta-exempt": "<reason>"}}, None),
    ({"summary": "delta-exempt:"}, None),
    ({"summary": "put `delta-exempt: <reason>` in the metadata to bypass this"}, None),
    ({}, None),
])
def test_delta_exempt(args, expected):
    assert plg.delta_exempt(args) == expected


def test_the_exemption_is_recorded_on_the_handoff(repo, home, card):
    _verdict(reason="changes requested")
    verdict = _handoff(summary="no code in this card",
                       metadata={"delta-exempt": "verification-only card"})
    assert verdict["action"] == "modify"
    assert verdict["args"]["metadata"]["review_delta_exempt"] == "verification-only card"


# --- the decision, as a pure function ---------------------------------------
@pytest.mark.parametrize("stat,exempt,blocked", [
    ("", None, True),
    (" app.py | 2 +-\n 1 file changed", None, False),
    (None, None, False),
    ("", "audit card", False),
])
def test_refusal_reason(stat, exempt, blocked):
    reason = plg.refusal_reason(rejected_head="abc1234567", head_now="abc1234567",
                                stat=stat, exempt=exempt)
    assert bool(reason) is blocked


def test_verdict_requires_the_recorded_run_to_have_landed():
    kw = dict(rejected_head="a" * 40, rejected_run_id=1821, head_now="a" * 40,
              stat="", exempt=None)
    assert plg.verdict(**kw, outcome_of_run="changes_requested") is not None
    assert plg.verdict(**kw, outcome_of_run="blocked") is None
    assert plg.verdict(**kw, outcome_of_run=None) is None


# --- the store seam ---------------------------------------------------------
def test_state_is_keyed_by_card_under_the_fleet_root(repo, home, card):
    plg._write_state("t_8dd715c6", {"task_id": "t_8dd715c6", "rejected_head": "b" * 40})
    path = plg.state_path("t_8dd715c6")
    assert path == home / "state" / "review-delta" / "t_8dd715c6.json"
    assert json.loads(path.read_text())["rejected_head"] == "b" * 40
    assert plg.read_state("t_8dd715c6")["rejected_head"] == "b" * 40
    assert plg.read_state("t_nope") == {}


def test_a_profile_shaped_hermes_home_still_anchors_the_root(tmp_path, monkeypatch):
    """mint-guard's lesson, inverted: bob writes the record and rodge must read it, so
    the anchor is the fleet ROOT even when the worker's HERMES_HOME is its own profile."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "bob"))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    assert plg.state_path("t_x") == tmp_path / "root" / "state" / "review-delta" / "t_x.json"


def test_resolve_card_reads_the_kernel(repo, tmp_path, monkeypatch):
    """The one store read, against the real kanban kernel and a real worktree card —
    including the spawn-path fallback where the env carries the workspace but no task id."""
    root = tmp_path / "kbroot"
    (root / "kanban").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(root / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    getattr(kb, "_INITIALIZED_PATHS", {}).clear()

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="a worktree card", assignee="bob",
                             workspace_kind="worktree", workspace_path=str(repo))

    by_id = plg.resolve_card({"task_id": tid})
    assert by_id is not None and by_id["id"] == tid
    assert by_id["workspace"] == str(repo) and by_id["workspace_kind"] == "worktree"

    # No task_id in the args and none in the env: the unique workspace match carries it.
    by_workspace = plg.resolve_card({})
    assert by_workspace is not None and by_workspace["id"] == tid

    # A scratch card is out of scope even when it is named explicitly.
    with kbc.connect_closing() as conn:
        scratch = kb.create_task(conn, title="a scratch card", assignee="bob",
                                 workspace_kind="scratch")
    assert plg.resolve_card({"task_id": scratch}) is None


def test_the_guard_stays_out_of_the_way_when_the_store_is_unreadable(monkeypatch):
    """A broken board read must allow, never refuse."""
    def boom(args):
        raise RuntimeError("store down")

    monkeypatch.setattr(plg, "resolve_card", boom)
    # resolve_card itself already swallows every read failure (it is stubbed here so the
    # failure is certain); what this pins is that on_pre_tool_call turns ANY failure into
    # "allow" rather than a refusal.
    assert plg.on_pre_tool_call(tool_name="kanban_request_review", args={"summary": "x"}) is None

