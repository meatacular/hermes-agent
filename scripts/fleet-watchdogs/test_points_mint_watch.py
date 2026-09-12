"""Controls for points-mint-watch. Each positive is paired with the case that
proves it can fail — the reverted core version's own tests could not, because the
placeholder and the assertion were written together.
"""
import importlib.util
import pathlib
import sqlite3
import time

import pytest

_spec = importlib.util.spec_from_file_location(
    "pmw", pathlib.Path(__file__).with_name("points-mint-watch.py"))
pmw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pmw)


@pytest.fixture()
def board(tmp_path):
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, created_at INT);"
        "CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT, author TEXT, body TEXT, created_at INT);")
    c.commit()
    return c


def add(c, tid, *, mins_ago=1, status="todo", comment=None):
    c.execute("INSERT INTO tasks VALUES (?,?,?,?)",
              (tid, f"card {tid}", status, int(time.time()) - mins_ago * 60))
    if comment is not None:
        c.execute("INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,?)",
                  (tid, "someone", comment, int(time.time())))
    c.commit()


def ids(rows):
    return sorted(t for t, _ in rows)


def test_flags_a_fresh_card_with_no_estimate(board):
    add(board, "t_a")
    assert ids(pmw.unestimated(board, int(time.time()) - 3600)) == ["t_a"]


def test_leaves_a_card_that_already_has_one(board):
    add(board, "t_b", comment="points-estimate: 3\nsized by the specifier")
    assert pmw.unestimated(board, int(time.time()) - 3600) == []


def test_recognises_its_own_placeholder_so_it_is_idempotent(board):
    add(board, "t_c", comment=pmw.comment_body())
    assert pmw.unestimated(board, int(time.time()) - 3600) == []


def test_ignores_cards_older_than_the_window(board):
    add(board, "t_d", mins_ago=600)
    assert pmw.unestimated(board, int(time.time()) - 3600) == []


def test_ignores_archived_cards(board):
    add(board, "t_e", status="archived")
    assert pmw.unestimated(board, int(time.time()) - 3600) == []


def test_the_comment_it_writes_is_what_the_LEDGER_parses():
    """The whole point. A placeholder the metric cannot see moves no number."""
    m = pmw.PTS_RE.search(pmw.comment_body(4))
    assert m and int(m.group(1)) == 4
    assert "auto-points" in pmw.comment_body()


def test_control_a_comment_without_the_marker_does_NOT_satisfy_it(board):
    add(board, "t_f", comment="this card is probably about 3 points, roughly")
    assert ids(pmw.unestimated(board, int(time.time()) - 3600)) == ["t_f"], \
        "prose must not count — only the parseable marker does"
