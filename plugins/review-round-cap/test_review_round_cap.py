"""Unit tests for review-round-cap's decision surface (card t_eb7a5c8c).

The end-to-end demonstration — the real plugin, the real `pre_tool_call` dispatcher, a copy
of the live board — lives in the card's workspace (`cap_demo.py`) and is the evidence for
AC2/AC3/AC4. These tests cover the pure parts, so a later edit to the arithmetic or the
marker reader fails loudly instead of quietly moving the ceiling.

    ~/.hermes/hermes-agent/venv/bin/python3 -m pytest plugins/review-round-cap/test_review_round_cap.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("review_round_cap_under_test", _HERE / "__init__.py")
assert _spec is not None and _spec.loader is not None
rrc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rrc)


# --- the marker reader ------------------------------------------------------
def test_a_comment_starting_with_the_marker_grants_one_round():
    assert rrc.grants_on_record(
        ["review-round-extension: default +1 — outstanding 11 -> 9 -> 5"]
    ) == (1, ["default"])


def test_the_marker_is_read_from_the_first_non_blank_line_only():
    # A leading blank line is fine; the marker is still the comment's own opening.
    assert rrc.grants_on_record(["\n  \nreview-round-extension: karl +1 — 4 -> 2"])[0] == 1


def test_a_comment_that_merely_mentions_the_marker_grants_nothing():
    """The mint-guard lesson: a body that QUOTES the marker must not switch the cap off.

    The guard's own refusal text quotes it, and so does the card's explanation of how to
    authorise a round.
    """
    bodies = [
        "overwatch: to bypass, post a comment reading "
        "`review-round-extension: default +1 — <evidence>`",
        "review-round-cap: post `review-round-extension: <profile> +1 — <evidence>`",
        "note:\nreview-round-extension: default +1 — indented, so not the opening line",
    ]
    assert rrc.grants_on_record(bodies) == (0, [])


def test_placeholder_grantors_are_not_grants():
    assert rrc.grants_on_record(["review-round-extension: <who> +1 — <evidence>"]) == (0, [])
    assert rrc.grants_on_record(["review-round-extension: {profile} +1 — ..."]) == (0, [])


def test_a_marker_without_a_count_grants_nothing():
    assert rrc.grants_on_record(["review-round-extension: default — please"]) == (0, [])


def test_a_typo_count_is_capped_not_honoured():
    total, _ = rrc.grants_on_record(["review-round-extension: default +99 — whatever"])
    assert total == rrc.GRANT_MAX


def test_grants_accumulate_and_name_their_grantors():
    assert rrc.grants_on_record([
        "review-round-extension: default +1 — run A: 11 -> 9 -> 5",
        "review-round-extension: bob +1 — run B: 5 -> 2",
    ]) == (2, ["default", "bob"])


# --- the arithmetic ---------------------------------------------------------
def test_rounds_not_bounces_cap_counts_rounds():
    """cap=3 permits rounds 1..3 = bounces 1 and 2; the THIRD bounce is refused."""
    assert rrc.decision(cap=3, bounces=0, granted=0) is None   # round 1 -> opens round 2
    assert rrc.decision(cap=3, bounces=1, granted=0) is None   # round 2 -> opens round 3
    assert rrc.decision(cap=3, bounces=2, granted=0) is not None  # round 3 == the cap


def test_each_authorisation_buys_exactly_one_round():
    assert rrc.decision(cap=3, bounces=2, granted=1) is None      # the granted round
    assert rrc.decision(cap=3, bounces=3, granted=1) is not None  # grant spent
    assert rrc.decision(cap=3, bounces=3, granted=2) is None      # second grant
    assert rrc.decision(cap=3, bounces=4, granted=2) is not None


def test_cap_of_one_is_a_card_with_no_rework_round():
    assert rrc.decision(cap=1, bounces=0, granted=0) is not None


def test_the_reason_names_the_round_that_would_have_opened():
    assert "round 4" in rrc.decision(cap=3, bounces=2, granted=0)


# --- the config read --------------------------------------------------------
def test_a_malformed_config_falls_back_to_the_documented_default(monkeypatch):
    import hermes_cli.config as config  # noqa: PLC0415

    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": "not a dict"})
    assert rrc.configured_cap() == rrc.DEFAULT_MAX_REVIEW_ROUNDS


def test_a_positive_value_is_read_verbatim(monkeypatch):
    import hermes_cli.config as config  # noqa: PLC0415

    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"max_review_rounds": 5}})
    assert rrc.configured_cap() == 5


def test_a_nonsense_value_is_clamped_not_obeyed(monkeypatch):
    import hermes_cli.config as config  # noqa: PLC0415

    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"max_review_rounds": 0}})
    assert rrc.configured_cap() == 1


def test_the_default_is_three_and_is_documented():
    assert rrc.DEFAULT_MAX_REVIEW_ROUNDS == 3
    yaml_text = (_HERE / "plugin.yaml").read_text()
    assert "kanban.max_review_rounds" in yaml_text and "default, 3" in yaml_text


# --- the hook is wired ------------------------------------------------------
def test_the_hook_ignores_every_other_tool():
    assert rrc.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_x"}) is None


def test_an_unreadable_board_allows_the_call(monkeypatch):
    """Fail-OPEN: a guard that stops the review lane is worse than the cap it replaces."""
    monkeypatch.setattr(rrc, "resolve_run", lambda args: {"id": "t_x", "status": "running",
                                                          "bounces": 99, "grant_bodies": []})
    monkeypatch.setattr(rrc, "record_refusal", lambda *a, **k: True)
    monkeypatch.setattr(rrc, "configured_cap", lambda: 3)
    assert rrc.on_pre_tool_call(tool_name="kanban_request_changes",
                                args={"task_id": "t_x"})["action"] == "block"
    monkeypatch.setattr(rrc, "configured_cap", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert rrc.on_pre_tool_call(tool_name="kanban_request_changes",
                                args={"task_id": "t_x"}) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
