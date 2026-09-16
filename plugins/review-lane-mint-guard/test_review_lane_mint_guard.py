from __future__ import annotations

import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "review_lane_mint_guard", Path(__file__).with_name("__init__.py"))
assert _SPEC is not None and _SPEC.loader is not None
plg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(plg)


def _create(**args):
    return plg.on_pre_tool_call(tool_name="kanban_create", args=args)


def test_ac2_review_card_names_review_lane_route_before_dispatch():
    result = _create(title="Review wave 1", assignee="rodge", body="review")
    assert result is not None
    assert result["action"] == "block"
    assert "kanban_request_review" in result["message"]
    assert "changes-requested" in result["message"]


def test_ac3_ordinary_implementation_card_is_unchanged():
    assert _create(title="Implement wave 1", assignee="bob", body="build") is None


def test_ac3_non_create_tool_is_unchanged():
    assert plg.on_pre_tool_call(
        tool_name="kanban_request_changes", args={"reason": "real blocker"}
    ) is None


def test_ac3_non_reviewer_review_title_is_unchanged():
    assert _create(title="Review wave 1", assignee="bob", body="build") is None
