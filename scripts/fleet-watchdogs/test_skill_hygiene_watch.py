"""skill-hygiene-watch: every positive paired with the control that proves it can fail."""
import importlib.util
import json
import pathlib
import sqlite3

import pytest

_spec = importlib.util.spec_from_file_location(
    "shw", pathlib.Path(__file__).with_name("skill-hygiene-watch.py"))
shw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shw)


# ------------------------------------------------------------------ has_trigger

@pytest.mark.parametrize("d", [
    "Use when taking a change from branch to merged PR — commit, open, watch CI, merge.",
    "Use BEFORE completing a card: confirm the change reached the live runtime.",
    "Read FIRST on any card in the BackupBrain repo — its layout and commands.",
    "Trigger whenever a review card is stuck.",
])
def test_a_description_that_states_when(d):
    assert shw.has_trigger(d)


@pytest.mark.parametrize("d", [
    "GitHub PR lifecycle: branch, commit, open, CI, merge.",
    "Verify a done card reached live runtime.",
    "Dark-themed SVG architecture/cloud/infra diagrams as HTML.",
    "|",            # the real broken-YAML case found on 2026-09-13
    "",
    "   ",
])
def test_CONTROL_a_description_that_states_only_what_it_is(d):
    assert not shw.has_trigger(d)


# --------------------------------------------------------------------- evaluate

def _sk(**kw):
    base = {"bytes": 4000, "description": "Use when you need the thing.", "dangling": []}
    base.update(kw)
    return base


def test_CONTROL_a_healthy_skill_reports_nothing():
    out = shw.evaluate("bob", {"good": _sk()}, disabled=set(), attached=set())
    assert out == []


def test_oversized_is_caught():
    out = shw.evaluate("bob", {"huge": _sk(bytes=46003)}, set(), set())
    assert [k for k, _s, _d in out] == ["oversized"]


def test_a_description_without_a_trigger_is_caught():
    out = shw.evaluate("bob", {"x": _sk(description="PDF files: create, read, merge.")}, set(), set())
    assert [k for k, _s, _d in out] == ["description"]


def test_dangling_reference_is_caught():
    out = shw.evaluate("bob", {"x": _sk(dangling=["gone.md"])}, set(), set())
    assert out[0][0] == "dangling-ref" and "gone.md" in out[0][2]


def test_disabled_and_attached_is_the_crash_risk():
    out = shw.evaluate("bob", {"tdd": _sk()}, disabled={"tdd"}, attached={"tdd"})
    assert [k for k, _s, _d in out] == ["crash-risk"]


def test_CONTROL_disabled_but_never_attached_is_fine():
    """That is the whole point of disabling never-opened skills — it must not nag."""
    assert shw.evaluate("bob", {"ascii": _sk()}, disabled={"ascii"}, attached={"tdd"}) == []


def test_a_disabled_skill_is_not_also_judged_on_size_or_description():
    out = shw.evaluate("bob", {"x": _sk(bytes=99999, description="nope")}, {"x"}, set())
    assert out == []


# ----------------------------------------------------------------------- render

def test_render_is_empty_when_clean():
    assert shw.render({"bob": [], "karl": []}) == ""


def test_crash_risk_is_hoisted_to_the_top():
    out = shw.render({"bob": [("description", "a", "x")],
                      "karl": [("crash-risk", "tdd", "disabled but attached")]})
    assert out.index("CRASH RISK") < out.index("[description]")
    assert "skill-update" in out


# ------------------------------------------------------------- gather, on disk

def _skill(root, category, name, desc, body="", refs=(), present=()):
    d = root / "skills" / category / name
    (d / "references").mkdir(parents=True, exist_ok=True)
    for f in present:
        (d / "references" / f).write_text("x")
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: \"{desc}\"\n---\n{body}\n"
                                + "".join(f"see references/{r}\n" for r in refs))
    return d


def dangling_of(body, own=frozenset(), others=None):
    """shw.dangling_refs with the profile map defaulted."""
    return shw.dangling_refs(body, set(own), dict(others or {}))


def test_skills_of_reads_size_description_and_dangling(tmp_path):
    _skill(tmp_path, "devops", "a", "Use when X happens.",
           refs=["there.md", "missing.md"], present=["there.md"])
    got = shw.skills_of(tmp_path)
    assert set(got) == {"a"}
    assert got["a"]["description"] == "Use when X happens."
    assert got["a"]["dangling"] == ["missing.md"]
    assert got["a"]["bytes"] > 0


# ------------------------------------------------------------- dangling_refs
# Every case below is a line that actually appears in a live SKILL.md on 2026-09-13.
# The first live run reported all four as dangling; all four were valid.

def test_own_reference_that_is_missing_is_dangling():
    assert dangling_of("see references/gone.md", own=set()) == ["gone.md"]


def test_own_reference_that_exists_is_not_dangling():
    assert dangling_of("see references/there.md", own={"there.md"}) == []


def test_skill_view_naming_another_skill_resolves_against_that_skill():
    body = 'skill_view(name="kanban-ops", file_path="references/gridlock.md")'
    assert dangling_of(body, own=set(), others={"kanban-ops": {"gridlock.md"}}) == []


def test_a_routing_arrow_naming_another_skill_resolves_against_it():
    body = "- `hermes-agent` -> `references/background-systems.md` - kanban model."
    assert dangling_of(body, own=set(), others={"hermes-agent": {"background-systems.md"}}) == []


def test_an_absolute_path_into_another_skill_resolves_against_it():
    body = "- `/Users/w/.hermes/skills/business/weroll-rider-persona/references/knowledge_search.md`"
    assert dangling_of(body, own=set(),
                       others={"weroll-rider-persona": {"knowledge_search.md"}}) == []


def test_the_longest_matching_skill_name_wins():
    """`weroll-knowledge` is a prefix of `weroll-knowledge-search`; the longer one owns it."""
    body = "weroll-knowledge-search now includes `references/knowledge_search.md`"
    assert dangling_of(body, own=set(),
                       others={"weroll-knowledge": set(),
                               "weroll-knowledge-search": {"knowledge_search.md"}}) == []


def test_a_skill_name_on_another_line_does_not_absolve_the_mention():
    """The control. A name on the previous line is not a qualifier -- that width is exactly
    how the first draft let one mention's name swallow the next mention's missing file."""
    body = "kanban-ops owns the board.\nsee references/gridlock.md"
    assert dangling_of(body, own=set(), others={"kanban-ops": {"gridlock.md"}}) == ["gridlock.md"]


def test_a_one_letter_skill_name_does_not_match_every_line():
    """The control for whole-word matching: skill `a` must not own `see references/x.md`."""
    assert dangling_of("see references/x.md", own=set(), others={"a": {"x.md"}}) == ["x.md"]


def test_a_named_skill_that_really_lacks_the_file_is_still_reported():
    body = 'skill_view(name="kanban-ops", file_path="references/nope.md")'
    assert dangling_of(body, own=set(), others={"kanban-ops": {"gridlock.md"}}) == ["nope.md"]


def test_skills_of_does_not_flag_a_real_cross_skill_pointer(tmp_path):
    """End to end through the walker, not just the pure helper."""
    _skill(tmp_path, "devops", "kanban-ops", "Use when working the board.",
           present=["gridlock.md"])
    d = _skill(tmp_path, "devops", "skill-update", "Use when changing a skill.")
    (d / "SKILL.md").write_text(
        "---\nname: skill-update\ndescription: \"Use when changing a skill.\"\n---\n"
        'skill_view(name="kanban-ops", file_path="references/gridlock.md")\n'
        "see references/own-missing.md\n")
    got = shw.skills_of(tmp_path)
    assert got["skill-update"]["dangling"] == ["own-missing.md"]
    assert got["kanban-ops"]["dangling"] == []


def test_support_dirs_are_not_mistaken_for_skills(tmp_path):
    d = _skill(tmp_path, "devops", "a", "Use when X.")
    (d / "references" / "SKILL.md").write_text("---\nname: nope\n---\n")
    assert set(shw.skills_of(tmp_path)) == {"a"}


def test_disabled_of_parses_the_real_config_shape(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "toolsets:\n- kanban\nskills:\n  auto_load:\n  - hermes-agent\n"
        "  disabled:   # dated comment\n  - apple\n  - comfyui\napprovals:\n  mode: \"off\"\n")
    got = shw.disabled_of(tmp_path)
    assert got == {"apple", "comfyui"}
    assert "hermes-agent" not in got          # the sibling auto_load list must not bleed in


def test_disabled_of_is_empty_without_a_skills_block(tmp_path):
    (tmp_path / "config.yaml").write_text("toolsets:\n- kanban\n")
    assert shw.disabled_of(tmp_path) == set()


def test_attached_skills_reads_the_board(tmp_path):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("create table tasks (skills text)")
    con.executemany("insert into tasks values (?)",
                    [(json.dumps(["tdd", "plan"]),), (None,), ("[]",), ("not json",)])
    con.commit()
    assert shw.attached_skills(tmp_path) == {"tdd", "plan"}


def test_attached_skills_without_a_board_is_empty_not_a_crash(tmp_path):
    assert shw.attached_skills(tmp_path) == set()


# -------------------------------------------------------------------- main loop

def test_main_is_silent_when_clean_and_loud_when_not(tmp_path, capsys, monkeypatch):
    _skill(tmp_path, "devops", "good", "Use when you need the thing.")
    monkeypatch.setattr(shw, "STATE", tmp_path / "s.json")
    import sys
    monkeypatch.setattr(sys, "argv", ["x", "--home", str(tmp_path), "--no-state"])
    shw.main()
    assert capsys.readouterr().out.strip() == ""          # CONTROL
    _skill(tmp_path, "devops", "bad", "A thing that does stuff.")
    shw.main()
    assert "[description] bad" in capsys.readouterr().out


def test_an_unchanged_finding_set_is_not_repeated(tmp_path, capsys, monkeypatch):
    _skill(tmp_path, "devops", "bad", "A thing that does stuff.")
    monkeypatch.setattr(shw, "STATE", tmp_path / "s.json")
    import sys
    monkeypatch.setattr(sys, "argv", ["x", "--home", str(tmp_path)])
    shw.main()
    assert "finding" in capsys.readouterr().out
    shw.main()
    assert capsys.readouterr().out.strip() == ""
