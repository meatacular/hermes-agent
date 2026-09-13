"""Calibration set for the mint guard: every card minted on 2026-09-12, real titles.

The three BLOCK cases are the day's actual defects. The ALLOW cases are every other
card of that day — the false-positive calibration. A guard that blocks a legitimate
card stops the board, which is worse than the defect it prevents.
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "mint_guard", pathlib.Path(__file__).with_name("__init__.py"))
mg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mg)

# (title, assignee) — the real mis-routings of 2026-09-12.
MUST_BLOCK = [
    ("Build: orgagent module + GET /api/orgagent/health + test", "rodge"),
    ("Fix brain search agent grounding regressions", "axel"),
    ("[platform] Plumb BACKUPBRAIN_API_KEY into disposable backend env", "axel"),
]

# Every other card minted that day, plus the shapes that must stay legal.
MUST_ALLOW = [
    ("Build: orgagent module + GET /api/orgagent/health + test", "bob"),
    ("Review: orgagent PR against AC 1-7", "rodge"),
    ("QA live: AC 3, 4, 5, 6 against the served backend", "steve-o"),
    ("Deploy: squash-merge orgagent PR + restart backend + confirm", "default"),
    ("[Bob] Apply E1 promotion: 300s on axel/switch/global config + restart", "bob"),
    ("[Rodge] Review E1 promotion manifest: 3-way check on 3 config files", "rodge"),
    ("[Steve-o] QA E1 promotion: real-config read + cost sanity", "steve-o"),
    ("Hermes Uplift Spec - decompose into clean, self-contained delivery stages", "jobsy"),
    ("Promote E1 (idle_compact_after_seconds=600) fleet-wide", "jobsy"),
    ("Verify BACKUPBRAIN_API_KEY plumbing fixes brain-search regression", "steve-o"),
    ("Plumb BACKUPBRAIN_API_KEY into disposable backend env", "bob"),
    ("Regression failure after job t_79986e81: fix and extend tests/regression", "bob"),
    ("+28d recheck: E1 promotion verdict (300s fleet-wide)", "default"),
    ("Estimation coverage at mint path (charter S4)", "default"),
    # shapes that must not trip it
    ("Fix brain search agent grounding regressions", ""),          # no assignee
    ("", "axel"),                                                   # no title
    ("Fix something", "someone-unknown"),                           # unknown name: core parks it
]


@pytest.mark.parametrize("title,assignee", MUST_BLOCK)
def test_real_misroutings_are_refused(title, assignee):
    assert mg.verdict(title, assignee) is not None, f"should have blocked: {title!r} -> {assignee}"


@pytest.mark.parametrize("title,assignee", MUST_ALLOW)
def test_legitimate_cards_pass(title, assignee):
    r = mg.verdict(title, assignee)
    assert r is None, f"FALSE POSITIVE on {title!r} -> {assignee}: {r}"


def test_override_stands_the_guard_down():
    t, a = MUST_BLOCK[0]
    assert mg.verdict(t, a) is not None
    assert mg.verdict(t, a, body="assignee-override: rodge is covering bob this week") is None


def test_hook_only_fires_on_kanban_create():
    assert mg.on_pre_tool_call(tool_name="kanban_complete",
                               args={"title": MUST_BLOCK[0][0], "assignee": "axel"}) is None
    out = mg.on_pre_tool_call(tool_name="kanban_create",
                              args={"title": MUST_BLOCK[1][0], "assignee": "axel"})
    assert out and out.get("action") == "block"


def test_hook_fails_open_on_a_broken_payload():
    assert mg.on_pre_tool_call(tool_name="kanban_create", args=None) is None
    assert mg.on_pre_tool_call() is None


def test_negative_control_the_suite_can_go_red(monkeypatch):
    """Neuter the decision and prove the BLOCK assertions would fail.

    Without this, a guard that returned None for everything would pass every
    ALLOW test and we would never know the BLOCK tests were vacuous.
    """
    monkeypatch.setattr(mg, "verdict", lambda *a, **k: None)
    failures = [t for t, a in MUST_BLOCK if mg.verdict(t, a) is not None]
    assert not failures            # neutered: nothing blocks
    monkeypatch.undo()
    still = [t for t, a in MUST_BLOCK if mg.verdict(t, a) is None]
    assert not still, f"guard is vacuous for: {still}"


# --- kernel-patch rule (2026-09-12) -----------------------------------------
# Calibrated on the REAL bodies of that day. The two "must not block" cases are the
# reason this rule is narrow: the batch's own anchor card quotes kernel paths as
# existing behaviour and then forbids editing them, and another card mentions a kernel
# file only to describe what two OTHER cards did. A guard that refused those would
# stop the board, which is worse than the defect it prevents.

KERNEL_MUST_BLOCK = [
    ("t_557ee139", "Bob — patch archive_task", "bob",
     "Patch `archive_task()` in `kanban_db.py` to enforce operator_hold on archive.\n\n"
     "**Location:** /Users/x/.hermes/hermes-agent/hermes_cli/kanban_db.py, line 3684."),
    ("t_9aef6c8b", "Patch dispatch-gap regressions", "bob",
     "## Goal\nPatch the two regressions introduced into `tools/kanban_tools.py` and "
     "`hermes_cli/kanban_decompose.py` by the --theirs resolution."),
    ("no assignee is not a bypass", "x", "",
     "Patch `archive_task()` in `hermes_cli/kanban_db.py`."),
    ("unknown assignee is not a bypass", "x", "nobody-real",
     "Modify tools/kanban_tools.py to add a lint."),
]

KERNEL_MUST_ALLOW = [
    ("t_fd2f708d anchor — names kernel paths as CONTEXT, then forbids editing them",
     "[platform] Routing-defect fix", "jobsy",
     "Root cause: `kanban_create` (tools/kanban_tools.py:878-913) passes assignee verbatim; "
     "the auto-decomposer (hermes_cli/kanban_decompose.py:57) rewrites only null/unknown.\n\n"
     "This parent is the approval anchor. No code lands in this card.\n\n"
     "## Do not\nDo not modify tools/kanban_tools.py or hermes_cli/kanban_decompose.py."),
    ("t_1974f630 — a kernel file named only to describe OTHER cards",
     "[platform] Two deploy cards", "bob",
     "Both touch `hermes_cli/kanban_db.py`. Neither conflicted — that was luck."),
    ("a nested tools/ path in ANOTHER repo",
     "Build the export tool", "bob",
     "Add a helper in backend/app/tools/export.py and wire it into main.py."),
    ("the shape we WANT — a plugin card",
     "[Bob] Mint guard as a plugin", "bob",
     "Create plugins/kanban-mint-guard/__init__.py on the pre_tool_call hook. "
     "Do not touch tools/kanban_tools.py."),
    ("the shape we WANT — a watchdog card",
     "[Bob] Add core-patch-watch", "bob",
     "Create scripts/fleet-watchdogs/core-patch-watch.py and a cron entry."),
    ("an explicitly approved kernel change",
     "Bob — approved kernel change", "bob",
     "core-patch-approved: Richie 2026-09-13\nPatch archive_task() in hermes_cli/kanban_db.py."),
]


@pytest.mark.parametrize("name,title,assignee,body", KERNEL_MUST_BLOCK)
def test_kernel_edits_are_refused(name, title, assignee, body):
    r = mg.verdict(title, assignee, body)
    assert r is not None and "kernel" in r, f"should have blocked: {name}"


@pytest.mark.parametrize("name,title,assignee,body", KERNEL_MUST_ALLOW)
def test_merely_naming_a_kernel_path_is_not_an_edit(name, title, assignee, body):
    r = mg.verdict(title, assignee, body)
    assert r is None or "kernel" not in r, f"FALSE POSITIVE on {name}: {r}"


def test_control_the_kernel_rule_is_not_vacuous(monkeypatch):
    """Neuter the detector and prove the BLOCK cases stop blocking."""
    monkeypatch.setattr(mg, "kernel_edit_line", lambda body: None)
    still = [n for n, t, a, b in KERNEL_MUST_BLOCK
             if (mg.verdict(t, a, b) or "").find("kernel") >= 0]
    assert not still
    monkeypatch.undo()
    missed = [n for n, t, a, b in KERNEL_MUST_BLOCK
              if "kernel" not in (mg.verdict(t, a, b) or "")]
    assert not missed, f"rule is vacuous for: {missed}"


# --- verb-list widening (2026-09-14, runfix-20260914) ------------------------
# The real card the narrow list let through. t_dcaf62c1 proposed a change to
# hermes_cli/kanban_db.py with the verbs "Fix" and "Add"; neither was in the kernel
# verb list, so the guard passed it, the worker edited hermes_cli/profiles.py instead,
# and the patch was never committed — which also made it invisible to core-patch-watch,
# because that reads commits. Both holes are closed; this pins the mint-guard half.

DCAF62_BODY = """## Fix options

1. **Add a `smith` profile** (symlink/alias to `default` or standalone config)
2. **Fix the decomposer** to use `default` instead of `smith` in its deploy-role assignment
3. **Add creation-time mapping** in `kanban_db.py` to resolve unknown aliases
"""


def test_the_card_that_got_through_on_2026_09_13_is_now_refused():
    assert mg.kernel_edit_line(DCAF62_BODY) is not None


@pytest.mark.parametrize("verb", ["fix", "add", "create", "implement", "plumb",
                                  "repair", "restore", "scaffold", "migrate", "rework"])
def test_each_build_lane_verb_is_also_a_kernel_edit_verb(verb):
    assert mg.kernel_edit_line(f"{verb.capitalize()} the lint in hermes_cli/kanban_db.py") is not None


def test_kernel_verbs_cover_build_lane():
    """The one-directional invariant: every build-lane verb must also be a kernel-edit verb.

    Two verb lists live in this module on purpose (widening the LANE regex would re-route
    ordinary cards), but they may only drift in the safe direction. A build verb that is not
    a kernel verb is exactly the 2026-09-13 hole: a card can name the build lane and edit the
    kernel without this rule seeing it.
    """
    assert set(mg._BUILD_LANE_VERBS) <= {v.lower() for v in mg.EDIT_VERBS}


def test_widening_did_not_break_the_negation_or_the_path_anchor():
    # the two shapes that keep the rule usable — both regressions would stop the board
    assert mg.kernel_edit_line("Add a lint. Do not modify tools/kanban_tools.py") is None
    assert mg.kernel_edit_line("Add a helper in backend/app/tools/export.py") is None


def test_control_the_widening_is_not_vacuous(monkeypatch):
    """Restore the OLD narrow verb list and watch the new cases go green->red."""
    import re as _re
    narrow = _re.compile(r"^\s*(?:[-*+]\s*|\d+[.)]\s*|#+\s*)?(?:\*\*)?"
                         r"(patch|edit|modify|change|refactor|amend|update|revert)\b", _re.I)
    monkeypatch.setattr(mg, "EDIT_VERB_LINE", narrow)
    assert mg.kernel_edit_line(DCAF62_BODY) is None      # the old list really did miss it
    assert mg.kernel_edit_line("Patch hermes_cli/kanban_db.py") is not None   # and still worked otherwise
