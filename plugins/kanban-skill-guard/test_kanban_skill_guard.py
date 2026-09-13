"""kanban-skill-guard: synthetic fixtures + calibration against the live fleet.

Every positive is paired with the control that proves it can fail — the guard exists
because a check that only walks the filesystem said "allow" to a card that would crash.
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "ksg", pathlib.Path(__file__).with_name("__init__.py"))
ksg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ksg)

CONFIG = """\
toolsets:
- coding
- kanban
agent:
  max_turns: 120
skills:
  auto_load:
  - hermes-agent
  disabled:   # ctxbudget-20260913
  - ascii-art
  - comfyui
  - openhue
approvals:
  mode: "off"
"""


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    prof = home / "profiles" / "bob"
    (prof / "skills" / "creative" / "ascii-art").mkdir(parents=True)
    (prof / "skills" / "creative" / "ascii-art" / "SKILL.md").write_text("---\nname: ascii-art\n---\n")
    (prof / "skills" / "devops" / "kanban-ops").mkdir(parents=True)
    (prof / "skills" / "devops" / "kanban-ops" / "SKILL.md").write_text("---\nname: kanban-ops\n---\n")
    # installed AND disabled — the case the guard exists for
    (prof / "skills" / "creative" / "comfyui").mkdir(parents=True)
    (prof / "skills" / "creative" / "comfyui" / "SKILL.md").write_text("---\nname: comfyui\n---\n")
    (prof / "config.yaml").write_text(CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(prof))      # profile-scoped, as a worker runs
    return home


# ------------------------------------------------------------------ disabled_for

def test_reads_the_disabled_list(fleet):
    assert ksg.disabled_for("bob") == {"ascii-art", "comfyui", "openhue"}


def test_auto_load_is_not_mistaken_for_disabled(fleet):
    """The key above `disabled:` is a sibling list — it must not bleed in."""
    assert "hermes-agent" not in ksg.disabled_for("bob")


def test_a_sibling_key_after_the_list_ends_it(fleet):
    assert "off" not in ksg.disabled_for("bob") and "mode:" not in ksg.disabled_for("bob")


def test_unreadable_config_is_an_empty_set_not_a_crash(fleet):
    assert ksg.disabled_for("nobody-here") == set()


def test_config_without_a_skills_block(tmp_path, monkeypatch):
    prof = tmp_path / ".hermes" / "profiles" / "x"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("toolsets:\n- kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert ksg.disabled_for("x") == set()


# ----------------------------------------------------------------------- verdict

def test_blocks_an_installed_but_disabled_skill(fleet):
    m = ksg.verdict({"assignee": "bob", "skills": ["ascii-art"], "title": "t"})
    assert m and "ascii-art" in m and "exit 1" in m


def test_CONTROL_an_enabled_installed_skill_is_allowed(fleet):
    assert ksg.verdict({"assignee": "bob", "skills": ["kanban-ops"], "title": "t"}) is None


def test_CONTROL_disabled_but_not_installed_is_left_to_the_dispatcher(fleet):
    """openhue is in bob's disabled list but not on disk. The dispatcher's own check
    blocks absent skills correctly; firing here would double-block."""
    assert ksg.verdict({"assignee": "bob", "skills": ["openhue"], "title": "t"}) is None


def test_CONTROL_an_absent_skill_is_left_to_the_dispatcher(fleet):
    """Not our case: missing_skills_for() catches absent skills correctly and blocks
    them with a typed capability reason. Firing here would double-block."""
    assert ksg.verdict({"assignee": "bob", "skills": ["never-installed"], "title": "t"}) is None


def test_mixed_list_blocks_on_the_disabled_one_only(fleet):
    m = ksg.verdict({"assignee": "bob", "skills": ["kanban-ops", "comfyui"], "title": "t"})
    assert m and "comfyui" in m and "kanban-ops" not in m


def test_override_in_the_body_stands_the_guard_down(fleet):
    args = {"assignee": "bob", "skills": ["ascii-art"], "title": "t",
            "body": "skill-override: re-enabling it in the same batch"}
    assert ksg.verdict(args) is None


def test_no_assignee_is_not_our_business(fleet):
    assert ksg.verdict({"skills": ["ascii-art"], "title": "t"}) is None


def test_no_skills_named_is_not_our_business(fleet):
    assert ksg.verdict({"assignee": "bob", "title": "t"}) is None


def test_a_bare_string_skills_field_is_handled(fleet):
    assert ksg.verdict({"assignee": "bob", "skills": "ascii-art", "title": "t"})


# ------------------------------------------------------------------ hook contract

def test_only_the_guarded_tools_are_inspected(fleet):
    args = {"assignee": "bob", "skills": ["ascii-art"]}
    assert ksg.on_pre_tool_call(tool_name="kanban_create", tool_input=args)["action"] == "block"
    assert ksg.on_pre_tool_call(tool_name="terminal", tool_input=args) is None
    assert ksg.on_pre_tool_call(tool_name="kanban_complete", tool_input=args) is None


def test_non_dict_input_is_ignored(fleet):
    assert ksg.on_pre_tool_call(tool_name="kanban_create", tool_input="nope") is None


def test_CONTROL_fails_open_when_the_verdict_itself_raises(fleet, monkeypatch):
    monkeypatch.setattr(ksg, "verdict", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ksg.on_pre_tool_call(tool_name="kanban_create", tool_input={"assignee": "bob"}) is None


# -------------------------------------------------- calibration on the LIVE fleet

LIVE = pathlib.Path.home() / ".hermes"


@pytest.mark.skipif(not (LIVE / "profiles" / "bob" / "config.yaml").exists(),
                    reason="live fleet not present")
def test_live_fleet_calibration(monkeypatch):
    """The real defect and the real non-defect, against today's actual configs."""
    monkeypatch.setenv("HERMES_HOME", str(LIVE / "profiles" / "bob"))
    assert ksg.verdict({"assignee": "bob", "skills": ["ascii-art"], "title": "x"}), \
        "ascii-art is disabled on bob today — this must block"
    for kept in ("test-driven-development", "kanban-ops", "systematic-debugging"):
        assert ksg.verdict({"assignee": "bob", "skills": [kept], "title": "x"}) is None, \
            f"{kept} is enabled on bob and has been attached to real cards — must not block"


@pytest.mark.skipif(not (LIVE / "profiles" / "karl" / "config.yaml").exists(),
                    reason="live fleet not present")
def test_live_no_card_ever_attached_a_skill_this_would_block(monkeypatch):
    """The six skills ever attached to a card, across every lane, must all pass."""
    import json, sqlite3
    db = LIVE / "kanban.db"
    if not db.exists():
        pytest.skip("no board")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    attached = set()
    for (s,) in con.execute("select skills from tasks where skills is not null "
                            "and skills not in ('','[]','null')"):
        try:
            attached.update(json.loads(s))
        except Exception:
            pass
    for prof in ("bob", "rodge", "steve-o", "karl", "jobsy"):
        monkeypatch.setenv("HERMES_HOME", str(LIVE / "profiles" / prof))
        for name in sorted(attached):
            assert ksg.verdict({"assignee": prof, "skills": [name], "title": "x"}) is None, \
                f"{prof} + {name} would be blocked, but that skill has been attached to a real card"
