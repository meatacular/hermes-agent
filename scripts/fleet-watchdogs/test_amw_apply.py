"""Tests for assignee-mismatch-watch's WRITE path (--apply).

Detection was already covered by the script's own --selftest (18 cases). These
cover only what happens when the auditor is allowed to mutate the board, which
is the part that had never been exercised.
"""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "amw", os.path.join(HERE, "assignee-mismatch-watch.py"))
amw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amw)

SCHEMA = """
CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
                   status TEXT, block_kind TEXT, created_at INTEGER);
CREATE TABLE task_events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                   kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_comments(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                   author TEXT, body TEXT, created_at INTEGER);
CREATE TABLE task_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                   profile TEXT, status TEXT, outcome TEXT);
"""


def flag(task_id, status="todo", lane="build", expected="bob", mint="axel"):
    return {"id": task_id, "status": status, "lane": lane,
            "expected": expected, "mint": mint, "reason": "test fixture"}


class ApplyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.con = sqlite3.connect(self.tmp.name)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        os.unlink(self.tmp.name)

    def add(self, tid, status="todo", block_kind=None, runs=()):
        self.con.execute(
            "INSERT INTO tasks(id,title,body,assignee,status,block_kind,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (tid, f"[build] {tid}", "body", "axel", status, block_kind, int(time.time())))
        for profile, st, outcome in runs:
            self.con.execute(
                "INSERT INTO task_runs(task_id,profile,status,outcome) VALUES(?,?,?,?)",
                (tid, profile, st, outcome))
        self.con.commit()

    def status_of(self, tid):
        return self.con.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]

    def comments_on(self, tid):
        return self.con.execute(
            "SELECT COUNT(*) n FROM task_comments WHERE task_id=?", (tid,)).fetchone()["n"]

    # --- the thing it is for -------------------------------------------------
    def test_it_reports_and_comments_a_real_mismatch_without_blocking(self):
        self.add("t_bad")
        acted, refused = amw.apply_actions(self.con, [flag("t_bad")])
        self.assertEqual([c["id"] for c in acted], ["t_bad"])
        self.assertEqual(refused, [])
        self.assertEqual(self.status_of("t_bad"), "todo")
        self.assertEqual(self.comments_on("t_bad"), 1)

    def test_no_block_event_is_recorded(self):
        self.add("t_bad")
        amw.apply_actions(self.con, [flag("t_bad")])
        row = self.con.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
            ("t_bad",)).fetchone()
        self.assertIsNone(row)

    # --- the things it must NOT touch ---------------------------------------
    def test_a_RUNNING_card_is_never_blocked(self):
        """Blocking a live worker's card out from under it is the sharp edge."""
        self.add("t_live", status="running")
        acted, refused = amw.apply_actions(self.con, [flag("t_live", status="running")])
        self.assertEqual(acted, [])
        self.assertEqual(self.status_of("t_live"), "running")
        self.assertEqual(self.comments_on("t_live"), 0)

    def test_a_done_card_is_left_alone(self):
        self.add("t_done", status="done")
        acted, _ = amw.apply_actions(self.con, [flag("t_done", status="done")])
        self.assertEqual(acted, [])
        self.assertEqual(self.status_of("t_done"), "done")

    def test_a_card_held_for_the_operator_is_left_alone(self):
        self.add("t_held", status="blocked", block_kind="operator_hold")
        acted, _ = amw.apply_actions(self.con, [flag("t_held", status="blocked")])
        self.assertEqual(acted, [])

    def test_a_card_that_already_ran_to_completion_is_left_alone(self):
        self.add("t_ran", runs=[("bob", "finished", "completed")])
        acted, _ = amw.apply_actions(self.con, [flag("t_ran")])
        self.assertEqual(acted, [])

    def test_it_is_idempotent(self):
        self.add("t_bad")
        amw.apply_actions(self.con, [flag("t_bad")])
        acted, _ = amw.apply_actions(self.con, [flag("t_bad", status="blocked")])
        self.assertEqual(acted, [])
        self.assertEqual(self.comments_on("t_bad"), 1)

    # --- the blast-radius cap ------------------------------------------------
    def test_over_the_cap_it_writes_NOTHING(self):
        ids = [f"t_{i}" for i in range(5)]
        for t in ids:
            self.add(t)
        acted, refused = amw.apply_actions(
            self.con, [flag(t) for t in ids], max_apply=3)
        self.assertEqual(acted, [])
        self.assertEqual(len(refused), 5)
        for t in ids:
            self.assertEqual(self.status_of(t), "todo", f"{t} was written despite the cap")
            self.assertEqual(self.comments_on(t), 0)

    def test_exactly_at_the_cap_it_acts(self):
        ids = [f"t_{i}" for i in range(3)]
        for t in ids:
            self.add(t)
        acted, refused = amw.apply_actions(
            self.con, [flag(t) for t in ids], max_apply=3)
        self.assertEqual(len(acted), 3)
        self.assertEqual(refused, [])

    def test_the_cap_counts_only_ACTIONABLE_cards(self):
        """Ten flags, eight of them terminal -> two actionable, under the cap."""
        for i in range(8):
            self.add(f"t_done{i}", status="done")
        self.add("t_a"); self.add("t_b")
        flags = ([flag(f"t_done{i}", status="done") for i in range(8)]
                 + [flag("t_a"), flag("t_b")])
        acted, refused = amw.apply_actions(self.con, flags, max_apply=3)
        self.assertEqual(sorted(c["id"] for c in acted), ["t_a", "t_b"])
        self.assertEqual(refused, [])

    # --- controls ------------------------------------------------------------
    def test_CONTROL_the_default_apply_flag_is_OFF(self):
        """The safe state must be the default: a caller that forgets reports."""
        args = amw.main.__wrapped__ if hasattr(amw.main, "__wrapped__") else None
        # re-parse through the real main by calling it with --selftest is wrong;
        # instead assert the module constant and the parser default directly.
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                amw.main(["--days", "0", "--selftest"])
            except SystemExit:
                pass
        with open(os.path.join(HERE, "assignee-mismatch-watch.py"), encoding="utf-8") as source_file:
            src = source_file.read()
        self.assertIn('dest="apply", action="store_true", default=False', src)

    def test_NEGATIVE_CONTROL_the_suite_can_go_red(self):
        """If the cap were removed, test_over_the_cap would fail. Prove the
        harness actually observes writes by writing one and detecting it."""
        self.add("t_probe")
        self.assertEqual(self.status_of("t_probe"), "todo")
        amw._post_comment(self.con, flag("t_probe"))
        self.con.commit()
        self.assertEqual(self.comments_on("t_probe"), 1,
                         "the test harness cannot see a write — every other "
                         "assertion in this file is worthless")


class TopicTagBlindness(unittest.TestCase):
    """The hole the live plant exposed: an anchored verb match is defeated by a
    leading topic tag, so an ordinary '[build] implement ...' card was invisible."""

    def test_a_tagged_build_title_resolves_to_the_build_lane(self):
        self.assertEqual(
            amw.implied_lane("[build] implement the retry helper in backupbrain", "body"),
            "build")

    def test_the_untagged_form_still_works(self):
        self.assertEqual(
            amw.implied_lane("Implement the retry helper in backupbrain", "body"),
            "build")

    def test_a_parenthesised_tag_is_also_stripped(self):
        self.assertEqual(
            amw.implied_lane("(platform) implement the retry helper", "body"), "build")

    def test_CONTROL_an_owner_tag_still_routes_by_OWNER_not_by_verb(self):
        """[Bob] must resolve through embedded_owner, not the strip path."""
        self.assertEqual(amw.embedded_owner("[Bob] Implement live-flag check"), "bob")
        self.assertEqual(amw.implied_lane("[Bob] Implement live-flag check", ""), "build")

    def test_CONTROL_a_tag_alone_does_not_invent_a_lane(self):
        """Stripping must not make a lane appear where no verb exists."""
        self.assertIsNone(amw.implied_lane("[build] backupbrain", ""))
        self.assertIsNone(amw.implied_lane("[misc] thoughts on the weekend", ""))

    def test_CONTROL_strip_is_bounded(self):
        """A long bracketed span is not a tag and must not be eaten."""
        long_tag = "[" + "x" * 40 + "] implement the helper"
        self.assertEqual(amw._strip_topic_tag(long_tag), long_tag)


if __name__ == "__main__":
    unittest.main(verbosity=2)
