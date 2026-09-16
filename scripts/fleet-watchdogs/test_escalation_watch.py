"""Behavior contracts for escalation-watch decision delivery."""
import importlib.util
import json
import sqlite3
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "escalation_watch", Path(__file__).with_name("escalation-watch.py")
)
watch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watch)


def board(tmp_path, *, status="blocked", block_kind="needs_input", marker=None):
    db = tmp_path / "board.db"
    c = sqlite3.connect(db)
    c.executescript(
        """
        CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, block_recurrences INTEGER,
                            body TEXT, status TEXT, block_kind TEXT, created_at REAL);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
                                  payload TEXT, created_at REAL);
        CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, body TEXT);
        """
    )
    c.execute("INSERT INTO tasks VALUES ('t_decision', 'Choose a path', 'bob', 2, 'Evidence', ?, ?, 1)",
              (status, block_kind))
    c.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, 1)",
              ("t_decision", json.dumps({"reason": "pick option A\nwith evidence"})))
    if marker:
        c.execute("INSERT INTO task_comments(task_id, body) VALUES (?, ?)", ("t_decision", marker))
    c.commit()
    c.close()
    return db


def run(tmp_path, db):
    messages = []
    watch.DB = db
    watch.STATE = tmp_path / "state.json"
    watch.slack_dm = lambda text: messages.append(text)
    watch.main()
    return messages


def test_ac1_decision_block_reported_once_per_recurrence(tmp_path):
    messages = run(tmp_path, board(tmp_path))
    assert len(messages) == 1
    assert "t_decision" in messages[0]
    assert "pick option A with evidence" in messages[0]
    assert "unblock t_decision" in messages[0]
    assert run(tmp_path, tmp_path / "board.db") == []


def test_ac3_clean_board_silent(tmp_path):
    assert run(tmp_path, board(tmp_path, status="done")) == []


def test_ac4_ceiling_still_reports(tmp_path):
    messages = run(tmp_path, board(tmp_path, block_kind="fault_signal",
                                    marker="escalation-ceiling: hard stop"))
    assert len(messages) == 1
    assert "Escalation ceiling" in messages[0]
    assert "Decision needed" not in messages[0]


def test_ac4_ceiling_not_suppressed_by_prior_decision(tmp_path):
    db = board(tmp_path)
    assert len(run(tmp_path, db)) == 1
    c = sqlite3.connect(db)
    c.execute("UPDATE tasks SET block_kind='fault_signal'")
    c.execute("INSERT INTO task_comments(task_id, body) VALUES (?, ?)",
              ("t_decision", "escalation-ceiling: hard stop"))
    c.commit()
    c.close()
    messages = run(tmp_path, db)
    assert len(messages) == 1
    assert "Escalation ceiling" in messages[0]
