"""Calibration set for the mint guard: every card minted on 2026-09-12, real titles.

The three BLOCK cases are the day's actual defects. The ALLOW cases are every other
card of that day — the false-positive calibration. A guard that blocks a legitimate
card stops the board, which is worse than the defect it prevents.
"""
import importlib.machinery
import importlib.util
import pathlib
import subprocess
import tempfile
from pathlib import Path

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
    ("Fix something", "someone-unknown"),   # names no profile -> can never dispatch (applyq-002)
]

# Every other card minted that day, plus the shapes that must stay legal.
MUST_ALLOW = [
    ("Build: orgagent module + GET /api/orgagent/health + test", "bob"),
    ("QA live: AC 3, 4, 5, 6 against the served backend", "steve-o"),
    ("Deploy: squash-merge orgagent PR + restart backend + confirm", "default"),
    ("[Bob] Apply E1 promotion: 300s on axel/switch/global config + restart", "bob"),
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
]

MUST_ALLOW_WITH_OVERRIDE = [
    ("Review: orgagent PR against AC 1-7", "rodge"),
    ("[Rodge] Review E1 promotion manifest: 3-way check on 3 config files", "rodge"),
]


@pytest.mark.parametrize("title,assignee", MUST_BLOCK)
def test_real_misroutings_are_refused(title, assignee):
    assert mg.verdict(title, assignee) is not None, f"should have blocked: {title!r} -> {assignee}"


@pytest.mark.parametrize("title,assignee", MUST_ALLOW)
def test_legitimate_cards_pass(title, assignee):
    assert mg.verdict(title, assignee) is None, f"FALSE POSITIVE on {title!r} -> {assignee}"


@pytest.mark.parametrize("title,assignee", MUST_ALLOW_WITH_OVERRIDE)
def test_review_wave_cards_are_explicitly_allowed_with_override(title, assignee):
    assert mg.verdict(title, assignee) is not None
    assert mg.verdict(title, assignee, body="assignee-override: existing review lane") is None


def test_override_stands_the_guard_down():
    t, a = MUST_BLOCK[0]
    assert mg.verdict(t, a) is not None
    assert mg.verdict(t, a, body="assignee-override: rodge is covering bob this week") is None


def test_review_lane_requires_review_handoff():
    reason = mg.verdict("[WP9b] Rodge — Review merged branch vs AC1 + AC3", "rodge")
    assert reason is not None
    assert "review-lane" in reason


def test_topic_tagged_review_is_classified():
    assert mg.verdict("[WP9b] Rodge — Review merged branch", "rodge") is not None


def test_review_lane_message_names_the_review_handoff():
    out = mg.on_pre_tool_call(tool_name="kanban_create",
                              args={"title": "Review merged branch", "assignee": "rodge"})
    assert out and out["action"] == "block"
    assert "kanban_request_review(reviewer='rodge')" in out["message"]


def test_review_verdict_tools_are_not_intercepted():
    for tool_name in ("kanban_request_review", "kanban_request_changes", "kanban_complete"):
        assert mg.on_pre_tool_call(tool_name=tool_name,
                                   args={"title": "Review merged branch", "assignee": "rodge"}) is None


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


# --- phantom assignee (2026-09-14, applyq-002) -------------------------------
# The board's own create-time check skips cards created `blocked`; this rule does not.

def test_phantom_assignee_is_refused(monkeypatch):
    monkeypatch.setattr(mg, "_assignee_is_phantom", lambda a: a == "smith")
    r = mg.on_pre_tool_call(tool_name="kanban_create",
                            args={"title": "Deploy: ship it", "assignee": "smith",
                                  "body": "merge the PR", "initial_status": "blocked"})
    assert r and r.get("action") == "block"
    assert "no Hermes profile" in r["message"]


def test_a_known_profile_is_untouched(monkeypatch):
    monkeypatch.setattr(mg, "_assignee_is_phantom", lambda a: True)   # would fire if reached
    assert mg.verdict("Build: the thing", "bob", "write it") is None


def test_an_unknown_but_REAL_profile_is_untouched(monkeypatch):
    """Not in KNOWN is not the same as not existing — a new profile must still mint."""
    monkeypatch.setattr(mg, "_assignee_is_phantom", lambda a: False)
    assert mg.verdict("Build: the thing", "some-new-profile", "write it") is None


def test_control_the_rule_is_not_vacuous(monkeypatch):
    monkeypatch.setattr(mg, "_assignee_is_phantom", lambda a: True)
    assert mg.verdict("Build: the thing", "some-new-profile", "write it") is not None


def test_phantom_check_fails_OPEN_when_the_profile_layer_raises(monkeypatch):
    import builtins
    real = builtins.__import__
    def boom(name, *a, **k):
        if name == "hermes_cli.profiles":
            raise ImportError("simulated")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", boom)
    assert mg._assignee_is_phantom("definitely-not-a-profile") is False


def test_an_empty_assignee_is_not_a_phantom():
    assert mg._assignee_is_phantom("") is False
    assert mg._assignee_is_phantom(None) is False


# --- extension-point ladder (2026-09-16, ladder-20260916) ---------------------
# The ladder lived in SOUL prose only, so a card could declare a seam that is not on it and be
# dispatched like any other. The case for this rule is one real card, quoted verbatim below.
#
# Calibrated the same way as the kernel rule: a case per rung (the false-positive control) and
# one per refusal. The rungs get FOUR forms each, because the marker is written differently by
# different authors and a form the rule cannot read is a rung it silently drops.

LADDER_VALUES = ("plugin", "watchdog", "skill", "soul", "config")


def _forms(value):
    return [
        f"extension-point: {value}",                                            # bare
        f"`extension-point: {value}` — the hook is the seam",                    # inline code
        f"- Extension point: `{value}`",                                        # bullet, alias
        f"**Extension point:** `{value}`",                                      # bold label
        f"Held: platform card. `extension-point: {value}` — not project code.",  # mid-line
        f"## Where it goes\n\nExtension point: {value} — justification follows",  # heading ctx
    ]


@pytest.mark.parametrize("value", LADDER_VALUES)
def test_every_rung_of_the_ladder_is_allowed(value):
    for form in _forms(value):
        r = mg.verdict("platform: the thing", "default", form + "\n")
        assert r is None, f"FALSE POSITIVE on ladder value {value!r} in form {form!r}: {r}"


# `config/kernel` is the real one. "kernel"/"core" are the same declaration one word long.
# Sample size for what the board actually writes: these are the values that appear in card
# bodies. The value is read as the FIRST TOKEN after the colon (so a trailing justification
# is never mistaken for part of it), which is also why a spaced form like "config and kernel"
# is not in this list — it is a value nobody writes, and the first-token rule is documented.
OFF_LADDER = ("config/kernel", "kernel", "core", "plugin/core", "soul/kernel", "none",
              "smith", "config+kernel", "watchdog/kernel")


@pytest.mark.parametrize("value", OFF_LADDER)
def test_off_ladder_values_are_refused(value):
    r = mg.verdict("platform: the thing", "default", f"## Where it goes\n`extension-point: {value}`\n")
    assert r is not None, f"should have blocked: extension-point: {value}"
    assert r.startswith("extension-point"), f"wrong rule answered for {value!r}: {r}"
    assert value.split("/")[0].split("+")[0] in r, f"the refusal must name the value: {r}"


LADDER_MUST_ALLOW = [
    ("no marker at all — the fail-open case every existing card relies on",
     "Just a plain platform card body. `hermes_cli/kanban_db.py` is mentioned here and there.\n"),
    ("the marker quoted as a PLACEHOLDER, as the ladder itself is written down",
     "A card body declaring `extension-point: <value>` where `<value>` is not one of the five.\n"),
    ("a bare sentence ABOUT the marker, no backticks, not line-initial",
     "The ladder says the body must declare extension-point: the chosen seam, and justify it.\n"),
    ("several SANCTIONED rungs joined (self-improvement-review.py asks for this form)",
     "extension-point: soul-or-skill\n"),
    ("two sanctioned rungs, slash form", "- `extension-point: plugin|watchdog`\n"),
    ("a sanctioned rung with a trailing justification", "extension-point: plugin — a pre_tool_call hook.\n"),
    ("a sanctioned rung, trailing punctuation", "Extension point: config,\n"),
    ("a sanctioned rung in a blockquote", "> extension-point: skill\n"),
]


@pytest.mark.parametrize("name,body", LADDER_MUST_ALLOW)
def test_forms_that_must_stay_legal(name, body):
    r = mg.verdict("platform: the thing", "default", body)
    assert r is None, f"FALSE POSITIVE on {name}: {r}"


# Verbatim tail of the card that motivated the rule (t_331cb549, 2026-09-16). Note what the
# kernel rule cannot see: nothing here starts with an edit verb, the change is described
# mid-sentence and the marker is the author's own declaration.
T331_TAIL = (
    "`create()` with no `project` and `workspace_kind=\"worktree\"` still inherits the board "
    "project id where a board declares one (`kanban_db.py:1316`).\n"
    "4. The CLI reports the same error to a human (not only the tool layer).\n\n"
    "points-estimate: 3\n\n"
    "Held: platform-lane defect, Smith's call whether to fix here or fold into the "
    "routing/mint-guard work. `extension-point: config/kernel` — `hermes_cli/kanban_db.py`, "
    "not project code.\n"
)


def test_the_card_that_motivated_the_rule_is_now_refused():
    assert mg.kernel_edit_line(T331_TAIL) is None, "the kernel rule was never able to see this"
    assert mg.declared_extension_point(T331_TAIL) == "config/kernel"
    r = mg.verdict("[Smith — HELD] kanban_create silently drops an unresolvable project link",
                   "default", T331_TAIL)
    assert r is not None and r.startswith("extension-point"), r


T_A525_BODY = (
    "## Fix (extension point: kernel — hence HELD for Richie, not dispatched)\n"
    "Primary: sanitize the gate's child environment.\n"
)


def _load_old_reader():
    with tempfile.TemporaryDirectory() as td:
        old_path = Path(td) / "old_mint_guard.py"
        source = subprocess.run(
            ["git", "show", "67707c39ed3b^:plugins/kanban-mint-guard/__init__.py"],
            cwd=Path(__file__).parents[2], check=True, capture_output=True, text=True,
        ).stdout
        old_path.write_text(source)
        loader = importlib.machinery.SourceFileLoader("mint_guard_old", str(old_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        old = importlib.util.module_from_spec(spec)
        loader.exec_module(old)
        return old


def test_ac1_heading_parenthesis_calibration_and_old_reader_was_red():
    """The real t_a525bc8b declaration was invisible to the pre-change reader."""
    old = _load_old_reader()
    assert old.declared_extension_point(T_A525_BODY) is None
    assert mg.declared_extension_point(T_A525_BODY) == "kernel"
    out = mg.verdict("gate defect", "rodge", T_A525_BODY)
    assert out and "kernel" in out


def test_ac3_fenced_and_sentence_examples_are_not_declarations():
    assert mg.verdict("platform: the thing", "default", "```\n`extension-point: kernel`\n```") is None
    assert mg.verdict("platform: the thing", "default",
                      "A sentence uses `extension-point: config/kernel` as an example.") is None


def test_ac4_bare_in_sentence_marker_stays_unread():
    assert mg.declared_extension_point(
        "The phrase extension-point: kernel is documentation, not a declaration.") is None


def test_core_patch_approved_stands_the_ladder_rule_down():
    body = T331_TAIL + "\ncore-patch-approved: Richie 2026-09-16\n"
    assert mg.verdict("platform: the thing", "default", body) is None


def test_dropping_the_marker_is_a_real_escape_route():
    """The prose without the marker mints — this rule is about the DECLARATION, not the subject."""
    body = T331_TAIL.replace("`extension-point: config/kernel` — `hermes_cli/kanban_db.py`, "
                             "not project code.", "the seam for this one is the kernel.")
    assert mg.declared_extension_point(body) is None
    assert mg.verdict("platform: the thing", "default", body) is None


def test_the_residual_false_positive_is_the_documented_one():
    """A card QUOTING an off-ladder example in marker form is refused itself.

    Pinned deliberately: it is the price of reading the inline-code form at all, and reading
    that form is what catches t_331cb549. Documented in plugin.yaml, not a surprise. The case
    below is the REAL line from the rule's own card (t_05a5883c), which quotes the example it
    exists to refuse — the rule's author's own card is the residual, and that is the honest
    shape of it.
    """
    r = mg.verdict("platform: the extension-point ladder is prose", "default",
                   "* `extension-point: config/kernel` is a self-declaration that the author "
                   "could not find a sanctioned seam.\n")
    assert r is not None and r.startswith("extension-point")


def test_a_line_that_DEFINES_the_marker_declares_nothing():
    """The false positive the 914-card sweep found, verbatim from two real cards.

    Both cards define the marker with an EMPTY value inside the code span, so the backtick after
    the colon closes an OUTER span and the trailing prose is the sentence, not a value. Reading
    it as the value refused both cards, which is the false positive this rule least affords.
    """
    defining = [
        "- `extension-point:` declared, with the chosen seam justified against the alternatives "
        "in the list above.",                                                 # t_03560fba
        "- `extension-point:` declared and justified against the alternatives.",  # t_da58d620
        "`extension-point:` — declare it, and do not default to a core patch",
    ]
    for line in defining:
        assert mg.declared_extension_point(line) is None, f"FALSE POSITIVE: {line!r}"
        assert mg.verdict("platform: the thing", "default", line) is None

    # ...and the same code-span form WITH a value inside it is still read.
    assert mg.declared_extension_point("`extension-point: skill`") is None
    assert mg.declared_extension_point("`extension-point: config/kernel`") == "config/kernel"


def test_the_hook_refuses_and_says_where_to_go_instead():
    out = mg.on_pre_tool_call(tool_name="kanban_create",
                             args={"title": "platform: the thing", "assignee": "default",
                                   "body": T331_TAIL})
    assert out and out.get("action") == "block"
    msg = out["message"]
    assert "Richie" in msg, "the refusal must say where an off-ladder change goes"
    for rung in mg.EXTENSION_POINTS:
        assert rung in msg, f"the ladder in the message is missing {rung!r}"
    assert "core-patch-approved:" in msg, "the escape hatch must be stated"


def test_an_off_ladder_value_is_not_answered_with_the_core_message():
    """"config/kernel" contains "kernel"; the routing must key on the RULE, not the substring."""
    out = mg.on_pre_tool_call(tool_name="kanban_create",
                             args={"title": "platform: the thing", "assignee": "default",
                                   "body": T331_TAIL})
    assert "extension point" in out["message"]
    assert "instructs a change to upstream's kernel" not in out["message"]
    # ...and the kernel rule still gets its own message.
    out2 = mg.on_pre_tool_call(tool_name="kanban_create",
                              args={"title": "Bob — patch archive_task", "assignee": "bob",
                                    "body": "Patch `archive_task()` in `hermes_cli/kanban_db.py`."})
    assert "instructs a change to upstream's kernel" in out2["message"]


def test_control_the_ladder_rule_is_not_vacuous(monkeypatch):
    """Neuter the detector and watch every refusal in this section stop firing."""
    monkeypatch.setattr(mg, "declared_extension_point", lambda body: None)
    missed = [v for v in OFF_LADDER
              if mg.verdict("platform: the thing", "default",
                            f"`extension-point: {v}`") is not None]
    assert not missed, f"the rule fires from somewhere other than declared_extension_point: {missed}"
    monkeypatch.undo()
    still = [v for v in OFF_LADDER
             if mg.verdict("platform: the thing", "default", f"`extension-point: {v}`") is None]
    assert not still, f"rule is vacuous for: {still}"


def test_the_ladder_rule_fails_open_on_garbage():
    for body in (None, 0, "", "extension-point:", "extension-point: \n", "extension-point\n"):
        assert mg.declared_extension_point(body) is None
        assert mg.verdict("platform: the thing", "default", body) is None


def test_a_blob_body_is_read_not_skipped():
    """kanban.db holds a few BLOB bodies. Raising on one would fail OPEN at the hook — the
    guard would go quiet on exactly the card it cannot see, which is the worst outcome."""
    assert mg.declared_extension_point(b"`extension-point: config/kernel`") == "config/kernel"
    assert mg.verdict("platform: the thing", "default", b"`extension-point: kernel`") is not None
    assert mg.verdict("platform: the thing", "default", b"`extension-point: plugin`") is None
    assert mg._text(b"\xff\xfe not utf8") != ""      # never raises


def test_the_ladder_and_the_sovereign_vocabulary_agree():
    """The five rungs are the SOUL's five, in the SOUL's order. Drift here is the whole defect."""
    assert mg.EXTENSION_POINTS == ("plugin", "watchdog", "skill", "soul", "config")


# --- the escape hatch must name someone (2026-09-16) --------------------------
# Found while verifying this rule, on this rule's own card. `CORE_OVERRIDE in body.lower()` was a
# bare substring test, so a body that QUOTED the placeholder — `core-patch-approved: <who>`, which
# is how the refusal message and the card that commissioned this rule both write it — switched BOTH
# the kernel rule and the ladder rule off. The card documenting the hatch was the card bypassing it.

KERNEL_BODY = "Patch `archive_task()` in `hermes_cli/kanban_db.py` to enforce operator_hold."
LADDER_BODY = "`extension-point: config/kernel` — hermes_cli/kanban_db.py, not project code."


@pytest.mark.parametrize("quoted", [
    "core-patch-approved: <who>",
    "core-patch-approved: <who>\n",
    "See the message: add `core-patch-approved: <who>` and the guard stands down.",
    "core-patch-approved:",
    "core-patch-approved:   ",
    "core-patch-approved: ...",
])
def test_quoting_the_escape_hatch_does_not_open_it(quoted):
    body = f"{KERNEL_BODY}\n\n{quoted}\n"
    assert mg._core_approved(body) is False, f"a placeholder opened the hatch: {quoted!r}"
    assert mg.verdict("platform: the thing", "default", body) is not None
    body2 = f"{LADDER_BODY}\n\n{quoted}\n"
    r = mg.verdict("platform: the thing", "default", body2)
    assert r is not None and r.startswith("extension-point")


@pytest.mark.parametrize("approval", [
    "core-patch-approved: Richie 2026-09-16",
    "core-patch-approved: Richie",
    "Core-Patch-Approved:  Richie",
    "core-patch-approved:richie",
])
def test_a_named_approval_opens_the_hatch_for_both_rules(approval):
    assert mg._core_approved(approval) is True
    assert mg.verdict("platform: the thing", "default", f"{KERNEL_BODY}\n\n{approval}\n") is None
    assert mg.verdict("platform: the thing", "default", f"{LADDER_BODY}\n\n{approval}\n") is None


def test_the_real_card_that_found_this_is_now_read_by_both_rules():
    """t_05a5883c's own body, verbatim head: it quotes the marker AND the placeholder."""
    body = (
        "Proof from today: card t_331cb549 carries `extension-point: config/kernel` and named\n"
        "`hermes_cli/kanban_db.py` as its deliverable.\n\n"
        "`core-patch-approved: <who>`. Cards with no `extension-point:` line behave exactly as\n"
        "they do today.\n"
    )
    assert mg._core_approved(body) is False
    assert mg.verdict("platform: the extension-point ladder is prose", "default", body) is not None



# --- worktree base (2026-09-16, card t_305e022d) -----------------------------
# A `worktree` card is cut from the repo's CURRENT HEAD; the create path accepts no base ref.
# Calibrated on the live board: 372 real worktree cards, of which this rule refuses 19 — 17 of
# them the defect class (three cards committing onto one hand-cut branch, six WP5 cards on one
# consolidated branch, a card based on an unmerged branch). The narrower reader is the whole
# point: a naive "an existing branch name appears in the body" rule refuses 90 of those 372,
# because `main` is also an English word ("<main> element", "do not push to `main`").
#
# The tests inject the repo facts, so they never read the host's git or boards.

WT_HEAD = "main"
WT_REFS = {
    "main", "staging", "wt/t_296c6855",
    "backupbrain/t_b6ebc5ec-wp-c4-pre-meeting-briefing-generation-over",
    "backupbrain/wp5-entity-resolution-consolidated",
}


def _wt(repo="/repo/backupbrain"):
    return repo


def _wt_refs(_repo):
    return WT_HEAD, set(WT_REFS)


def _wt_args(body, **kw):
    args = {"title": "platform: the thing", "assignee": "bob", "body": body,
            "workspace_kind": "worktree"}
    args.update(kw)
    return args


def _wt_conflict(body, **kw):
    return mg.worktree_base_conflict(_wt_args(body, **kw), resolve_repo=_wt, repo_refs=_wt_refs)


# Real bodies, verbatim heads, from the cards this rule was measured against.
WT_MUST_REFUSE = [
    ("t_bd86565f declared  ", "Branch: `wt/t_296c6855` (already exists, already has 2 commits'"),
    ("t_6f4b7880 directive ", "- [ ] Commit on `wt/t_296c6855`: `fleet-watchdogs: add watcher`."),
    ("t_b264ec8b directive ", "The briefing generation lives on "
                              "`backupbrain/t_b6ebc5ec-wp-c4-pre-meeting-briefing-generation-over`, not `main`."),
    ("t_1a5ec739 directive ", "Work on `backupbrain/wp5-entity-resolution-consolidated` (PR #24)."),
    ("t_9463e540 base      ", "Base branch: staging"),
    ("origin-namespaced    ", "base: `origin/staging` @ `8000d7a`"),
    ("t_ecaa3434 own branch", "- Branch: `backupbrain/dnd-restore` off `backupbrain/owner-regression-hotfix`."),
]

WT_MUST_ALLOW = [
    ("no citation at all   ", "Implement the digest API. Run `scripts/regress.sh` and report."),
    ("cites HEAD (declared)", "- Branch: `main`\nDeliver by merging to `main`."),
    ("cites HEAD (directive)", "Work on `main`. Do not open a new branch."),
    ("cites HEAD via origin", "base: `origin/main` @ `8000d7a` — first command `git fetch origin`."),
    # the false-positive surface the narrow reader exists for — all real lines from the corpus
    ("prose about main     ", "= a 480px column inside the shell's `<main>`. Measured dead space: 586px."),
    ("negative delivery    ", "- Do not push to `main`/`origin/main` directly. Do not merge to `main`."),
    ("delivery target      ", "6. Push **`backupbrain/wp7-digest-consolidated`** and open **one PR**."),
    ("merge into main      ", "- Merge branch `backupbrain/wp5-entity-resolution-consolidated` into `main`."),
    ("cherry-pick source   ", "2. **Cherry-pick `830c204`** from branch `backupbrain/t_a4668501`."),
    ("wave table           ", "| `t_3eb013b8` | `backupbrain/t_3eb013b8-wp-c2-capture` @ `96f5d0d` | unified capture |"),
    ("runbook prose        ", "`t_50533bc1` closed done; the branch `wt/t_d4dd73ee` @ `6b3606c` (OAuth token service)."),
    ("bare word, not a ref ", "base: the parent commit\nBranch: TBD"),
    ("shell line           ", "BASE=$(git merge-base HEAD origin/main)\nBASE=http://localhost:$PORT"),
    ("placeholder          ", "- branch: `<branch>`\n- base: ..."),
    ("body is None         ", None),
]


@pytest.mark.parametrize("name,body", WT_MUST_REFUSE)
def test_a_worktree_card_that_must_sit_on_another_branch_is_refused(name, body):
    assert _wt_conflict(body) is not None, f"{name!r} should have been refused"


def test_the_refusal_names_the_branch_and_the_alternative_kind():
    reason = _wt_conflict("- Branch: `wt/t_296c6855`")
    assert "'wt/t_296c6855'" in reason
    assert "'main'" in reason, "the refusal must name the HEAD the card would actually get"


def test_the_message_names_the_branch_and_the_dir_remedy():
    msg = mg._message_for("platform: the thing", "bob", _wt_conflict("- Branch: `wt/t_296c6855`"))
    assert "`wt/t_296c6855`" in msg, "the offending branch must be named in the message"
    assert "`workspace_kind='dir'`" in msg
    assert "workspace_path" in msg
    assert "kernel change" in msg.lower(), "the forbidden route must be named as forbidden"


@pytest.mark.parametrize("name,body", WT_MUST_ALLOW)
def test_ordinary_worktree_cards_are_accepted(name, body):
    assert _wt_conflict(body) is None, f"FALSE POSITIVE on {name!r}: {body!r}"


def test_only_worktree_cards_are_in_scope():
    body = "- Branch: `wt/t_296c6855`\n- [ ] Commit on `wt/t_296c6855`."
    for kind in ("dir", "scratch", None, "DIR "):
        args = _wt_args(body)
        args["workspace_kind"] = kind
        assert mg.worktree_base_conflict(args, resolve_repo=_wt, repo_refs=_wt_refs) is None
    assert mg.worktree_base_conflict(_wt_args(body), resolve_repo=_wt, repo_refs=_wt_refs) is not None


def test_the_rule_fails_open_when_it_cannot_know_the_repo_or_the_head():
    body = "- Branch: `wt/t_296c6855`"
    # no repo resolvable (unknown project, no board metadata, ...)
    assert mg.worktree_base_conflict(_wt_args(body), resolve_repo=lambda a: None,
                                     repo_refs=_wt_refs) is None
    # detached HEAD / unreadable repo
    assert mg.worktree_base_conflict(_wt_args(body), resolve_repo=_wt,
                                     repo_refs=lambda r: (None, set())) is None
    # no body / garbage args
    assert mg.worktree_base_conflict(_wt_args(""), resolve_repo=_wt, repo_refs=_wt_refs) is None
    assert mg.worktree_base_conflict(None, resolve_repo=_wt, repo_refs=_wt_refs) is None
    assert mg.worktree_base_conflict({}, resolve_repo=_wt, repo_refs=_wt_refs) is None


def test_a_blob_body_citation_is_read_not_skipped():
    assert mg.branch_citations(b"- Branch: `wt/t_296c6855`") == [
        ("wt/t_296c6855", "declared", "- Branch: `wt/t_296c6855`")]
    assert _wt_conflict(b"- Branch: `wt/t_296c6855`") is not None
    assert mg.branch_citations(b"\xff\xfe not utf8") == []


def test_the_worktree_rule_does_not_need_an_assignee_and_is_not_a_routing_hatch():
    body = "- Branch: `wt/t_296c6855`"
    # no assignee, no title: the rule is about the DELIVERABLE, so it still fires
    assert mg.verdict("", "", body, args=_wt_args(body, assignee="")) is not None
    # ...and `assignee-override:` (a routing hatch) must not wave a wrong base through
    overridden = f"{body}\nassignee-override: rodge is covering bob"
    assert mg.verdict("platform: the thing", "rodge", overridden,
                      args=_wt_args(overridden)) is not None


def test_control_the_worktree_rule_is_not_vacuous(monkeypatch):
    """Neuter the citation reader and watch every refusal in this section stop firing."""
    monkeypatch.setattr(mg, "branch_citations", lambda body: [])
    missed = [b for _, b in WT_MUST_REFUSE if _wt_conflict(b) is not None]
    assert not missed, f"the rule fires from somewhere other than branch_citations: {missed}"
    monkeypatch.undo()
    still = [b for _, b in WT_MUST_REFUSE if _wt_conflict(b) is None]
    assert not still, f"rule is vacuous for: {still}"


def test_the_residual_false_positive_is_the_documented_one():
    """A card that DECLARES the branch it will itself create is refused too.

    At mint time that branch does not exist, so the card is one line of body text from being
    fine — the message names the three ways forward. Reading declarations only when the ref
    exists today would have dropped four of the seven true positives above, which are cards
    whose branch was merged and deleted after they ran. That trade is deliberate.
    """
    body = "- Branch: `backupbrain/p0-digest-content-pipeline-consolidated`."
    assert mg.branch_citations(body) == [
        ("backupbrain/p0-digest-content-pipeline-consolidated", "declared", body)]
    assert _wt_conflict(body) is not None


def test_the_hook_refuses_a_worktree_card_on_the_wrong_base(monkeypatch):
    monkeypatch.setattr(mg, "worktree_repo_for", _wt)
    monkeypatch.setattr(mg, "_repo_head_and_refs", _wt_refs)
    out = mg.on_pre_tool_call(tool_name="kanban_create", args=_wt_args(
        "**Base your branch on the approved briefing work, not `main`.** The briefing generation "
        "lives on `backupbrain/t_b6ebc5ec-wp-c4-pre-meeting-briefing-generation-over`."))
    assert out and out["action"] == "block"
    assert "worktree base" in out["message"]
    assert "`backupbrain/t_b6ebc5ec-wp-c4-pre-meeting-briefing-generation-over`" in out["message"]
    assert "`main`" in out["message"]
    # ...and the same body on a `dir` card is minted unchanged (AC4)
    ok = mg.on_pre_tool_call(tool_name="kanban_create",
                             args=_wt_args("Branch: `wt/t_296c6855`", workspace_kind="dir"))
    assert ok is None
    # ...and a genuine routing refusal is still a routing refusal, not a worktree message
    out2 = mg.on_pre_tool_call(tool_name="kanban_create",
                               args={"title": "Build the thing", "assignee": "rodge"})
    assert "review lane" in out2["message"] and "worktree base" not in out2["message"]


# ---------------------------------------------------------------------------------------------
# `dir` workspace rule (card t_a8fa2e12, 2026-09-17): an explicit `dir` path must be a populated
# git work tree. Absent path -> kernel defaults (fail open). scratch/worktree kinds untouched.
# ---------------------------------------------------------------------------------------------

def _dir_args(path, kind="dir", **kw):
    a = {"title": "[build] implement the thing", "assignee": "bob", "body": "do the thing",
         "workspace_kind": kind, "workspace_path": path}
    a.update(kw)
    return a


def test_dir_card_at_a_missing_path_is_refused(tmp_path):
    r = mg.dir_workspace_conflict(_dir_args(str(tmp_path / "nope")), repo_root_for=lambda p: p)
    assert r and r.startswith(mg.DIR_REASON_PREFIX) and "does not exist" in r


def test_dir_card_at_an_empty_directory_is_refused(tmp_path):
    d = tmp_path / "empty"; d.mkdir()
    r = mg.dir_workspace_conflict(_dir_args(str(d)), repo_root_for=lambda p: p)
    assert r and "EMPTY" in r


def test_dir_card_outside_any_git_repo_is_refused(tmp_path):
    d = tmp_path / "plain"; d.mkdir(); (d / "f").write_text("x")
    r = mg.dir_workspace_conflict(_dir_args(str(d)), repo_root_for=lambda p: None)
    assert r and "not inside a git repository" in r


def test_dir_card_in_a_populated_checkout_is_allowed(tmp_path):
    d = tmp_path / "repo"; d.mkdir(); (d / "f").write_text("x")
    assert mg.dir_workspace_conflict(_dir_args(str(d)), repo_root_for=lambda p: p) is None


def test_dir_rule_ignores_other_kinds_and_absent_paths(tmp_path):
    d = tmp_path / "empty"; d.mkdir()
    assert mg.dir_workspace_conflict(_dir_args(str(d), kind="scratch"), repo_root_for=lambda p: p) is None
    assert mg.dir_workspace_conflict(_dir_args(str(d), kind="worktree"), repo_root_for=lambda p: p) is None
    assert mg.dir_workspace_conflict(_dir_args("", kind="dir"), repo_root_for=lambda p: p) is None


def test_the_hook_refuses_an_empty_dir_card_with_the_dir_message(tmp_path, monkeypatch):
    d = tmp_path / "empty"; d.mkdir()
    out = mg.on_pre_tool_call(tool_name="kanban_create", args=_dir_args(str(d)))
    assert out and out["action"] == "block" and "A `dir` card tells the worker" in out["message"]


def test_control_the_dir_rule_is_not_vacuous(tmp_path, monkeypatch):
    """Neuter the rule and the same empty-dir card mints — proves the refusal above is the rule's."""
    d = tmp_path / "empty"; d.mkdir()
    monkeypatch.setattr(mg, "dir_workspace_conflict", lambda args, repo_root_for=None: None)
    assert mg.on_pre_tool_call(tool_name="kanban_create", args=_dir_args(str(d))) is None


def test_build_card_at_the_live_platform_checkout_is_refused(tmp_path, monkeypatch):
    live = tmp_path / ".hermes" / "hermes-agent"; live.mkdir(parents=True); (live / "f").write_text("x")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "bob"))   # a worker's home
    r = mg.dir_workspace_conflict(_dir_args(str(live), title="[build] fix the thing"), repo_root_for=lambda p: p)
    assert r and "LIVE platform checkout" in r


def test_review_card_at_the_live_platform_checkout_is_allowed(tmp_path, monkeypatch):
    live = tmp_path / ".hermes" / "hermes-agent"; live.mkdir(parents=True); (live / "f").write_text("x")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert mg.dir_workspace_conflict(_dir_args(str(live), title="[Rodge] review the thing"), repo_root_for=lambda p: p) is None
    assert mg.dir_workspace_conflict(_dir_args(str(live), title="[Verify] the thing is live"), repo_root_for=lambda p: p) is None


def test_second_build_card_at_a_shared_dir_is_refused(tmp_path):
    d = tmp_path / "shared"; d.mkdir(); (d / "f").write_text("x")
    rows = [("t_aaaa0001", "[build] implement the first thing")]
    r = mg.dir_workspace_conflict(_dir_args(str(d), title="[build] implement the second thing"), repo_root_for=lambda p: p, board_reader=lambda path: rows)
    assert r and "already carries a build-lane card in flight" in r and "t_aaaa0001" in r


def test_shared_dir_rule_ignores_non_build_neighbours_and_fails_open(tmp_path):
    d = tmp_path / "shared"; d.mkdir(); (d / "f").write_text("x")
    assert mg.dir_workspace_conflict(_dir_args(str(d), title="[build] implement x"), repo_root_for=lambda p: p, board_reader=lambda path: [("t_1", "[Rodge] review y")]) is None
    assert mg.dir_workspace_conflict(_dir_args(str(d), title="[Verify] x"), repo_root_for=lambda p: p, board_reader=lambda path: [("t_1", "[build] implement y")]) is None
    def boom(path): raise RuntimeError("board unreadable")
    assert mg.dir_workspace_conflict(_dir_args(str(d), title="[build] implement x"), repo_root_for=lambda p: p, board_reader=boom) is None


# --- rule 7: the mint-time tenant / project / base contract (2026-09-23, P2-mint-contract) ---
# The defect: an explicit `tenant` never binds the project (kanban_db.create_task fills
# project_id from the BOARD first, and reads the tenant map only for a project-less scratch
# card), so a `[WeRoll]` card minted on the default board — whose project is BackupBrain —
# got `backupbrain/t_…` as its branch in the weroll-app repo (t_32e1fb81, the third such card).
# Every fixture here is hermetic: a tmp fleet root with its own board.json files, a tmp
# tenants map, a tmp kanban.db. Nothing touches ~/.hermes.
import json as _json
import sqlite3 as _sqlite3

P7_BB = "/Users/x/Projects/backupbrain-anchor"
P7_BB_LEGACY = "/Users/x/Projects/backupbrain"
P7_WR = "/Users/x/Projects/weroll-app"
P7_TENANTS = {
    "backupbrain": {"id": "p_3d4a6fe1", "slug": "backupbrain", "name": "BackupBrain",
                    "primary_path": P7_BB, "ci_gate": {"repo": "meatacular/backupbrain"}},
    "backupbrain-legacy": {"id": "p_backupbrain_legacy", "slug": "backupbrain-legacy",
                           "name": "BackupBrain (legacy primary path)", "primary_path": P7_BB_LEGACY,
                           "ci_gate": {"repo": "meatacular/backupbrain"}},
    "weroll-app": {"id": "p_weroll_app", "slug": "weroll-app", "name": "WeRoll",
                   "primary_path": P7_WR, "ci_gate": {"repo": "meatacular/weroll-app"}},
}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A tmp fleet root: default board = BackupBrain (legacy default_workdir, exactly like the
    real one), `weroll` board = WeRoll, a tenants map, an empty board db. Returns the root."""
    root = tmp_path / "hermes"
    for slug, workdir, pid in (("default", P7_BB_LEGACY, "p_3d4a6fe1"), ("weroll", P7_WR, "p_weroll_app")):
        d = root / "kanban" / "boards" / slug
        d.mkdir(parents=True)
        (d / "board.json").write_text(_json.dumps(
            {"slug": slug, "default_workdir": workdir, "project_id": pid, "archived": False}))
    (root / "kanban-tenants.json").write_text(_json.dumps(P7_TENANTS))
    db = root / "kanban.db"
    conn = _sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, tenant TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_TENANTS", str(root / "kanban-tenants.json"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_TENANT", raising=False)
    return root


def _p7_hook(**args):
    return mg.on_pre_tool_call(tool_name="kanban_create", args=args)


def _p7_running_land(root, tenant="weroll-app", tid="t_running1"):
    conn = _sqlite3.connect(root / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES (?, ?, 'running', ?)",
                 (tid, "[Release] Land PR #21 (reviewed head aaa) — HELD for Richie", tenant))
    conn.commit()
    conn.close()


# (a) the defect: a WeRoll worktree card on the default (BackupBrain) board, no explicit project
def test_a_weroll_card_on_the_backupbrain_board_is_refused_and_told_what_to_pass(fleet):
    out = _p7_hook(title="[WeRoll] Build the LCP fix", assignee="bob", tenant="weroll-app",
                   workspace_kind="worktree", body="Do the thing.")
    assert out and out["action"] == "block"
    assert "tenant/project" in out["message"]
    assert "`p_3d4a6fe1`" in out["message"], "the project the kernel WOULD bind must be named"
    assert 'project="p_weroll_app"' in out["message"], "the fix must be spelled out"
    assert "--board weroll" in out["message"], "the tenant's own board must be offered"
    assert "backupbrain/t_" in out["message"], "the wrong branch namespace must be named"


def test_a_the_reason_also_fires_from_verdict_without_an_assignee(fleet):
    # a property of the deliverable: no assignee, no lane, still refused
    args = {"title": "[WeRoll] Recorded performance finding", "tenant": "weroll-app",
            "workspace_kind": "worktree", "body": "HELD"}
    assert mg.verdict("", "", "HELD", args=args).startswith("tenant/project")


def test_a_the_title_tag_alone_names_the_tenant(fleet):
    # t_32e1fb81's shape: `[WeRoll]` title, tenant given; but even with NO tenant arg the
    # business tag is read, because that is what a human reads
    args = {"title": "[WeRoll] Build it", "workspace_kind": "worktree", "body": ""}
    assert mg.tenant_project_conflict(args) is not None


def test_a_a_worktree_line_in_the_body_names_the_repo(fleet):
    args = {"title": "Build it", "workspace_kind": "worktree",
            "body": f"workspace: {P7_WR}\nDo it."}
    assert mg.tenant_project_conflict(args) is not None
    args["body"] = f"workspace: {P7_BB}\nDo it."
    assert mg.tenant_project_conflict(args) is None


# (b) NEGATIVE CONTROL: the same card with the explicit project passes
def test_b_negative_control_explicit_project_passes(fleet):
    out = _p7_hook(title="[WeRoll] Build the LCP fix", assignee="bob", tenant="weroll-app",
                   project="p_weroll_app", workspace_kind="worktree",
                   body="Do the thing.\nbranch-target: main")
    assert out is None
    # ...and on the tenant's own board, no explicit project needed
    out = _p7_hook(title="[WeRoll] Build the LCP fix", assignee="bob", tenant="weroll-app",
                   board="weroll", workspace_kind="worktree", body="x\nbranch-target: main")
    assert out is None


def test_b_control_the_rule_is_not_vacuous(fleet, monkeypatch):
    args = {"title": "[WeRoll] Build it", "tenant": "weroll-app", "workspace_kind": "worktree", "body": "x"}
    assert mg.verdict("[WeRoll] Build it", "bob", "x", args=args) is not None
    monkeypatch.setattr(mg, "tenant_project_conflict", lambda *a, **k: None)
    assert mg.verdict("[WeRoll] Build it", "bob", "x", args=args) is None, \
        "the refusal comes from somewhere other than rule 7a"


# (c) a BackupBrain card on the BackupBrain board passes — including via the legacy workdir
def test_c_a_backupbrain_card_on_the_backupbrain_board_passes(fleet):
    for extra in ({"tenant": "backupbrain"}, {}, {"tenant": "backupbrain-legacy"},
                  {"workspace_path": P7_BB + "/.worktrees/t_x"}):
        args = {"title": "[BackupBrain] Build the digest", "assignee": "bob",
                "workspace_kind": "worktree", "body": "x\nbranch-target: main", **extra}
        assert _p7_hook(**args) is None, f"FALSE POSITIVE with {extra}"


def test_c_out_of_scope_shapes_are_untouched(fleet):
    # explicit scratch / dir / project="" are not worktree cards the kernel would bind
    for extra in ({"workspace_kind": "scratch"}, {"workspace_kind": "dir", "workspace_path": P7_WR},
                  {"project": ""}):
        args = {"title": "[WeRoll] Build it", "tenant": "weroll-app", "body": "x", **extra}
        assert mg.tenant_project_conflict(args) is None, extra


# (d) branch-target injection
def test_d_missing_branch_target_is_injected_as_main(fleet):
    out = _p7_hook(title="[WeRoll] Build it", assignee="bob", tenant="weroll-app",
                   project="p_weroll_app", workspace_kind="worktree", body="Do it.")
    assert out == {"action": "modify", "args": {"body": "Do it.\n\nbranch-target: main\n"}}
    # an empty body gets just the line
    out = _p7_hook(title="Build it", assignee="bob", tenant="backupbrain", workspace_kind="worktree")
    assert out["args"]["body"] == "branch-target: main\n"


def test_d_present_branch_target_is_untouched_and_non_build_cards_get_none(fleet):
    for body in ("branch-target: main\nDo it.", "- **branch-target**: `develop`", "Do it.\nBranch_Target = main"):
        assert _p7_hook(title="[WeRoll] Build it", assignee="bob", tenant="weroll-app",
                        project="p_weroll_app", workspace_kind="worktree", body=body) is None, body
    # a review card is not a build card: nothing to inject
    assert _p7_hook(title="[WeRoll] Verify the LCP fix", assignee="steve-o", tenant="weroll-app",
                    project="p_weroll_app", workspace_kind="worktree", body="x") is None


def test_d_the_tenant_trunk_key_wins_over_main(fleet):
    tenants = _json.loads((fleet / "kanban-tenants.json").read_text())
    tenants["weroll-app"]["trunk"] = "develop"
    (fleet / "kanban-tenants.json").write_text(_json.dumps(tenants))
    out = _p7_hook(title="[WeRoll] Build it", assignee="bob", tenant="weroll-app",
                   project="p_weroll_app", workspace_kind="worktree", body="x")
    assert out["args"]["body"].endswith("branch-target: develop\n")


def test_d_injection_never_accompanies_a_refusal(fleet):
    out = _p7_hook(title="[WeRoll] Build it", assignee="bob", tenant="weroll-app",
                   workspace_kind="worktree", body="x")
    assert out["action"] == "block" and "args" not in out


# (e) land cards must be held
def test_e_a_land_card_without_a_hold_is_refused(fleet):
    out = _p7_hook(title="[Release] Land PR #22 (reviewed head 22730a0) — HELD for Richie",
                   assignee="default", tenant="weroll-app", project="p_weroll_app", body="Merge it.")
    assert out and out["action"] == "block"
    assert "land card" in out["message"] and "operator-hold: manual" in out["message"]
    # the marker at the END of the body (what `hold=true` alone would give) is not the contract
    out = _p7_hook(title="[Release] Land PR #22", assignee="default", tenant="weroll-app",
                   project="p_weroll_app", body="Merge it.\n\noperator-hold: manual\n")
    assert out and "first line" in out["message"]


def test_e_a_held_land_card_passes(fleet):
    for body in ("operator-hold: manual\n\nMerge it.", "\n  operator-hold: manual\nx",
                 "Merge after review.\nhold: wait-pr-merged 22", "hold: manual"):
        assert _p7_hook(title="[Release] Land PR #22 (reviewed head 22730a0)", assignee="default",
                        tenant="weroll-app", project="p_weroll_app", body=body) is None, body


def test_e_only_land_cards_are_in_scope():
    assert mg.land_card_conflict({"title": "[Release] Deploy the thing", "body": "x"}) is None
    assert mg.land_card_conflict({"title": "Land PR #22", "body": "x"}) is None


# (f) one running land card per tenant
def test_f_a_second_land_card_while_one_runs_is_refused(fleet):
    _p7_running_land(fleet)
    out = _p7_hook(title="[Release] Land PR #22 (reviewed head 22730a0)", assignee="default",
                   tenant="weroll-app", project="p_weroll_app", body="operator-hold: manual\n\nMerge it.")
    assert out and out["action"] == "block"
    assert "already running" in out["message"] and "t_running1" in out["message"]
    assert "land-serialisation: waived" in out["message"]


def test_f_the_waiver_and_another_tenant_pass(fleet):
    _p7_running_land(fleet)
    ok = _p7_hook(title="[Release] Land PR #22", assignee="default", tenant="weroll-app",
                  project="p_weroll_app",
                  body="operator-hold: manual\n\nland-serialisation: waived stacked PRs 21+22\nMerge it.")
    assert ok is None
    ok = _p7_hook(title="[Release] Land PR #9", assignee="default", tenant="backupbrain",
                  body="operator-hold: manual\n\nMerge it.")
    assert ok is None, "a BackupBrain land card is not serialised behind a WeRoll one"


def test_f_the_db_is_read_read_only_and_a_missing_db_fails_open(fleet, monkeypatch):
    _p7_running_land(fleet)
    args = {"title": "[Release] Land PR #22", "tenant": "weroll-app", "body": "operator-hold: manual"}
    assert mg.land_card_conflict(args) is not None
    monkeypatch.setenv("HERMES_KANBAN_DB", str(fleet / "nope.db"))
    assert mg.land_card_conflict(args) is None
    # a reader that raises is a pass, never a refusal
    def boom(t, b):
        raise RuntimeError("db locked")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(fleet / "kanban.db"))
    assert mg.land_card_conflict(args, board_reader=boom) is None


def test_f_control_the_serialisation_rule_is_not_vacuous(fleet):
    args = {"title": "[Release] Land PR #22", "tenant": "weroll-app", "body": "operator-hold: manual"}
    assert mg.land_card_conflict(args, board_reader=lambda t, b: []) is None
    assert mg.land_card_conflict(args, board_reader=lambda t, b: [("t_z", "[Release] Land PR #1")]) is not None


# (g) a broken tenants file fails open
def test_g_a_broken_tenants_file_fails_open(fleet):
    (fleet / "kanban-tenants.json").write_text("{not json")
    out = _p7_hook(title="[WeRoll] Build it", assignee="bob", tenant="weroll-app",
                   workspace_kind="worktree", body="x")
    # nothing refuses; the only thing left standing is the trunk default
    assert out is None or out["action"] == "modify"
    assert mg.tenant_project_conflict({"title": "[WeRoll] Build it", "tenant": "weroll-app",
                                       "workspace_kind": "worktree"}) is None
    (fleet / "kanban-tenants.json").unlink()
    assert mg.tenant_project_conflict({"title": "[WeRoll] Build it", "tenant": "weroll-app",
                                       "workspace_kind": "worktree"}) is None


def test_g_garbage_args_fail_open(fleet):
    for bad in (None, {}, {"title": None, "body": b"\xff", "workspace_kind": "worktree", "tenant": 3}):
        assert mg.tenant_project_conflict(bad) is None
        assert mg.land_card_conflict(bad) is None
        assert mg.branch_target_injection(bad) is None
