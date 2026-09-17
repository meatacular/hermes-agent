"""Calibration set for hold-marker-guard (card t_46783f69, Richie-approved 3A+B).

Four halves, and the order matters:

  * the DECISION cases are the contract — a held mint gains the marker, a non-held mint is
    untouched, a body that already declares a hold is never overruled, and every hostile
    payload mints unchanged;
  * the PARSER corpus pins this module's copy of the marker pattern against the REAL
    `release-operator-hold-watch.find_marker`, so a writer that drifts from the reader is a
    red test rather than a silent release;
  * the CHAIN case drives the kernel's own `pre_tool_call` dispatch and then asks the real
    watchdog for a verdict on the row that came out of it — the guarded card must not appear
    in the release lines, while an identical card created the pre-fix way must;
  * the NEGATIVE CONTROL neuters the decision and proves the add-cases would fail, so the
    green run above cannot be vacuous.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_spec = importlib.util.spec_from_file_location(
    "hold_marker_guard", pathlib.Path(__file__).with_name("__init__.py"))
plg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plg)

WATCHER = pathlib.Path.home() / ".hermes" / "scripts" / "release-operator-hold-watch.py"
BODY = "Richie supplies the Slack apps and tokens per workspace."


def _watcher():
    """The real watchdog, loaded as a module (its own main() is __main__-guarded)."""
    if not WATCHER.is_file():
        pytest.skip(f"release-operator-hold-watch.py not found at {WATCHER}")
    spec = importlib.util.spec_from_file_location("rohw_for_hold_marker_guard", WATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the decision -----------------------------------------------------------
def test_a_held_mint_gains_the_marker_on_its_own_line():
    add = plg.verdict({"hold": True, "body": BODY})
    assert add == {"body": f"{BODY}\n\noperator-hold: manual\n"}
    # on its OWN line, which is the only place the reader looks
    assert add["body"].splitlines()[-1] == "operator-hold: manual"


def test_a_held_mint_with_no_body_still_gets_the_marker():
    assert plg.verdict({"hold": True}) == {"body": "operator-hold: manual\n"}
    assert plg.verdict({"hold": True, "body": "   "}) == {"body": "operator-hold: manual\n"}


@pytest.mark.parametrize("hold", [True, "true", "TRUE", "1", "yes", " yes "])
def test_every_hold_spelling_the_tool_accepts_arms_the_guard(hold):
    assert plg.verdict({"hold": hold, "body": BODY}) is not None


@pytest.mark.parametrize("hold", [None, False, "false", "0", "no", "", "maybe", 0])
def test_everything_else_is_left_exactly_as_minted(hold):
    assert plg.verdict({"hold": hold, "body": BODY}) is None


def test_a_control_mint_without_hold_is_untouched():
    """The control the card's verification table names: no `hold=True`, no marker."""
    args = {"title": "Build: orgagent module", "assignee": "bob", "body": BODY}
    assert plg.verdict(args) is None
    assert plg.on_pre_tool_call(tool_name="kanban_create", args=args) is None


def test_the_guard_is_idempotent():
    once = plg.verdict({"hold": True, "body": BODY})["body"]
    assert plg.verdict({"hold": True, "body": once}) is None


def test_a_declared_sequencing_wait_is_never_overruled():
    """`dependency-wait` is an author saying "this releases itself" — adding `manual`
    would turn a wait into a sticky gate, which the 2026-09-15 rule forbids."""
    body = "waiting for the build parent\n\noperator-hold: dependency-wait\n"
    assert plg.verdict({"hold": True, "body": body}) is None


def test_mid_line_prose_is_not_a_declaration_so_the_guard_still_speaks():
    """The minting card's own prose quoting the token must not be mistaken for a marker."""
    body = f"{BODY}\n\ndo NOT add `operator-hold: manual` to that card.\n"
    assert plg.verdict({"hold": True, "body": body}) is not None


def test_the_hook_only_answers_kanban_create():
    for tool in ("kanban_complete", "kanban_block", "kanban_comment", "kanban_request_review"):
        assert plg.on_pre_tool_call(tool_name=tool, args={"hold": True, "body": BODY}) is None
    out = plg.on_pre_tool_call(tool_name="kanban_create", args={"hold": True, "body": BODY})
    assert out and out["action"] == "modify"
    assert out["args"]["body"].rstrip().endswith("operator-hold: manual")
    # modify, never block: this guard cannot refuse a card
    assert "message" not in out


def test_the_hook_fails_open_on_every_broken_payload():
    assert plg.on_pre_tool_call(tool_name="kanban_create", args=None) is None
    assert plg.on_pre_tool_call(tool_name="kanban_create", args="hold=true") is None
    assert plg.on_pre_tool_call(tool_name="kanban_create", args={"hold": True, "body": 7}) is not None
    assert plg.on_pre_tool_call() is None
    assert plg.on_pre_tool_call(tool_name=None, args={"hold": True}) is None


# --- the parser, pinned to the reader --------------------------------------
CORPUS = [
    BODY,
    "",
    None,
    "operator-hold: manual\n",
    "operator-hold: manual",
    "- operator-hold: manual\n",
    "* operator-hold: manual\n",
    "+ operator-hold: manual\n",
    "1. operator-hold: manual\n",
    "1) operator-hold: manual\n",
    "> operator-hold: manual\n",
    "## operator-hold: manual\n",
    "**operator-hold: manual**\n",
    "__operator-hold: manual__\n",
    "`operator-hold: manual`\n",
    "operator-hold:manual\n",
    "   operator-hold :   manual\n",
    "OPERATOR-HOLD: MANUAL\n",
    "operator-hold: dependency-wait\n",
    "operator-hold: manual-approval\n",
    "operator-hold: manualish\n",
    "do NOT add `operator-hold: manual` to this card\n",
    "the marker is `operator-hold: manual` today\n",
    "some text\noperator-hold: manual\nmore\n",          # line 2 of 3: still line-anchored
    "some text and then operator-hold: manual\n",        # mid-line: NOT a marker
    "operator-hold: manual\noperator-hold: dependency-wait\n",
    "operator-hold: dependency-wait\noperator-hold: manual\n",
]


@pytest.mark.parametrize("body", CORPUS, ids=lambda b: repr(b)[:44])
def test_the_effective_parser_decides_exactly_as_the_watchdog_does(body):
    """Whatever parser this process is using — the kernel helper once item 006 lands, this
    module's copy before that — it must answer what the READER answers, token for token."""
    assert plg.marker_of(body) == _watcher().find_marker(body)


def test_the_plugin_knows_when_it_is_delegating_to_the_kernel_helper():
    parser, source = plg.canonical_parser()
    assert source in {"hermes_cli.hold_marker", "hold-marker-guard"}
    assert parser is not None


# --- the real chain, and the real watchdog ---------------------------------
def _isolated_invocation(tmp_path, monkeypatch):
    """A temp fleet root + temp kanban DB, and the kernel's own dispatch."""
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

    root = tmp_path / "hold_marker_root"
    (root / "kanban").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(root / "kanban.db"))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    from hermes_cli import kanban_db as kb
    getattr(kb, "_INITIALIZED_PATHS", {}).clear()
    return _dispatch_pre_tool_call_hooks, kb, root


def _read_body(root, tid):
    import sqlite3
    conn = sqlite3.connect(str(root / "kanban.db"))
    try:
        row = conn.execute("SELECT body, block_kind, status FROM tasks WHERE id = ?",
                           (tid,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _strip_marker(root, tid):
    """Rewrite a row to the PRE-FIX state: held, minted with no marker at all."""
    import sqlite3
    conn = sqlite3.connect(str(root / "kanban.db"))
    try:
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (BODY, tid))
        conn.commit()
    finally:
        conn.close()


def test_a_held_mint_survives_the_real_watchdog(tmp_path, monkeypatch):
    """The wiring, not just the decision: the kernel's dispatch, the loader's plugin, real DB
    rows, and then the real watchdog's verdict on those rows.

    Two cards, one variable. Both bodies are identical and both cards end up
    `blocked`/`operator_hold`; only the first carries the marker, because only the first came
    out of the mint path. The second is rewritten to the pre-fix state — held, minted with no
    marker, which is what the board carried when this defect was measured. The watchdog must
    release the second and never the first.

    (Note the second card is created through the kernel, which stamps it too — that is the
    other half of the 3A+B ruling showing up, and it is why the control has to be built by
    rewriting the row rather than by minting it. Each half is then tested where it alone can
    be: this file for the hook, `tests/hermes_cli/test_hold_marker_kernel.py` for the kernel.)
    """
    dispatch, kb, root = _isolated_invocation(tmp_path, monkeypatch)
    from hermes_cli import kanban_db_connect as kbc

    args = {"title": "Slack apps + tokens per workspace", "assignee": "default",
            "hold": True, "body": BODY}
    block, modified = dispatch("kanban_create", dict(args))
    assert block is None, "hold-marker-guard must never block a mint"
    assert modified and "body" in modified

    with kbc.connect_closing() as conn:
        guarded = kb.create_task(conn, title=args["title"], assignee="default",
                                 body=modified["body"], initial_status="blocked",
                                 block_kind="operator_hold")
        # The pre-fix row: a held card whose body carries no marker.
        control = kb.create_task(conn, title=args["title"], assignee="default",
                                 body=BODY, initial_status="blocked",
                                 block_kind="operator_hold")
    _strip_marker(root, control)

    body, kind, status = _read_body(root, guarded)
    assert status == "blocked" and kind == "operator_hold"
    assert _watcher().find_marker(body) == "manual", "the marker did not reach the row"
    assert _watcher().find_marker(_read_body(root, control)[0]) is None

    env = dict(os.environ, RELEASE_HOLD_DRYRUN="1",
               RELEASE_HOLD_DB=str(root / "kanban.db"),
               RELEASE_HOLD_STATE=str(root / "hold-watch-state.json"))
    out = subprocess.run([sys.executable, str(WATCHER)], capture_output=True, text=True,
                         env=env, timeout=60).stdout
    assert f"RELEASE [no_parents] {control}" in out, out
    assert guarded not in out, f"the guarded card was released:\n{out}"
    assert "1 released, 1 held" in out, out


# --- the suite can go red ---------------------------------------------------
def test_negative_control_the_suite_can_go_red(monkeypatch):
    """Neuter the decision and prove the add-cases fail.

    Without this, a guard that returned None for everything would pass every
    "left untouched" test and we would never know the add-cases were vacuous.
    """
    monkeypatch.setattr(plg, "verdict", lambda *a, **k: None)
    assert plg.verdict({"hold": True, "body": BODY}) is None      # neutered: nothing added
    monkeypatch.undo()
    assert plg.verdict({"hold": True, "body": BODY}) is not None
