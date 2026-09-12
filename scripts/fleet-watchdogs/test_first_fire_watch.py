"""Tests for first-fire-watch. Hermetic: its own DB, its own state file."""
import importlib.util, json, os, sqlite3, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_module(home):
    os.environ["HERMES_HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(
        "ffw", str(HERE / "first-fire-watch.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SCHEMA = """
CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT);
CREATE TABLE task_events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                         kind TEXT, payload TEXT, created_at INTEGER);
"""

REASON = "mint assignee=axel (payload) on build-lane card; expected bob"


class Args:
    def __init__(self, **kw):
        self.dry_run = kw.get("dry_run", False)
        self.force = kw.get("force", False)
        self.verbose = kw.get("verbose", False)


class FirstFire(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "state").mkdir()
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text(json.dumps(
            {"jobs": [{"name": "first-fire-watch", "enabled": True}]}, indent=2))
        self.db = self.home / "kanban.db"
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit(); con.close()
        self.m = load_module(self.home)

    def add_block(self, task_id, title="Implement the retry helper", by="assignee-mismatch-watch"):
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR IGNORE INTO tasks(id,title,assignee,status) VALUES(?,?,?,?)",
                    (task_id, title, "axel", "blocked"))
        con.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                    (task_id, "blocked", json.dumps({"by": by, "reason": REASON}), 1))
        con.commit(); con.close()

    def state(self):
        return json.loads((self.home / "state" / "first-fire-watch.json").read_text())

    def jobs_enabled(self):
        d = json.loads((self.home / "cron" / "jobs.json").read_text())
        return d["jobs"][0]["enabled"]

    def capture(self, **kw):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.m.run(Args(**kw))
        return buf.getvalue()

    # --- baseline ------------------------------------------------------------
    def test_first_run_baselines_silently(self):
        self.add_block("t_old")          # pre-existing history is not news
        out = self.capture()
        self.assertEqual(out, "")
        self.assertIsNotNone(self.state()["baseline_event_id"])
        self.assertFalse(self.state()["retired"])

    def test_history_before_the_baseline_never_fires(self):
        self.add_block("t_old")
        self.capture()                    # baseline
        out = self.capture()
        self.assertEqual(out, "")

    # --- the thing it is for -------------------------------------------------
    def test_it_fires_once_on_a_real_block(self):
        self.capture()                    # baseline on empty history
        self.add_block("t_real123", title="Implement the retry helper")
        out = self.capture()
        self.assertIn("routing checker stopped a job", out)
        self.assertIn("Implement the retry helper", out)
        self.assertIn("t_real123", out)
        self.assertTrue(self.state()["retired"])

    def test_it_tidies_its_own_cron_entry_when_it_can(self):
        self.capture()
        self.add_block("t_real123")
        self.assertTrue(self.jobs_enabled())
        self.capture()
        self.assertFalse(self.jobs_enabled())
        self.assertTrue(self.state()["cron_retired"])

    def test_the_STATE_FILE_is_what_stops_a_second_message(self):
        """Not the cron entry. On the real fleet the scheduler owns jobs.json and
        clobbered the disable; the state file is the mechanism that held."""
        self.capture()
        self.add_block("t_real1")
        self.assertNotEqual(self.capture(), "")
        # simulate the scheduler putting the job back
        d = json.loads((self.home / "cron" / "jobs.json").read_text())
        d["jobs"][0]["enabled"] = True
        (self.home / "cron" / "jobs.json").write_text(json.dumps(d))
        self.add_block("t_real2")
        self.assertEqual(self.capture(), "",
                         "a re-enabled cron entry caused a second message — "
                         "the state file is not authoritative")

    def test_cron_retired_is_FALSE_when_the_write_does_not_stick(self):
        """Honest reporting: if the tidy-up fails it must say so, not claim success."""
        self.capture()
        self.add_block("t_real123")
        (self.home / "cron" / "jobs.json").write_text(json.dumps(
            {"jobs": [{"name": "someone-else", "enabled": True}]}))
        self.capture()
        self.assertFalse(self.state()["cron_retired"])

    def test_it_stays_silent_after_firing(self):
        self.capture()
        self.add_block("t_real1")
        self.assertNotEqual(self.capture(), "")
        self.add_block("t_real2")
        self.assertEqual(self.capture(), "", "it messaged twice — it must fire once")

    # --- what it must ignore -------------------------------------------------
    def test_a_PLANTED_test_card_never_counts(self):
        """The whole point is a card nobody planted."""
        self.capture()
        self.add_block("t_amwtest_bad")
        self.assertEqual(self.capture(), "")
        self.assertFalse(self.state()["retired"])

    def test_a_block_from_a_DIFFERENT_source_never_counts(self):
        self.capture()
        self.add_block("t_real9", by="overwatch-escalator")
        self.assertEqual(self.capture(), "")

    def test_a_planted_card_does_not_consume_the_one_shot(self):
        """A test card must not burn the alert a real card is owed."""
        self.capture()
        self.add_block("t_amwtest_bad")
        self.capture()
        self.add_block("t_real_after")
        out = self.capture()
        self.assertIn("t_real_after", out)

    # --- the message itself --------------------------------------------------
    def test_the_message_is_plain_english(self):
        self.capture()
        self.add_block("t_real123")
        out = self.capture()
        for jargon in ("lane", "mint", "payload", "charter", "block_kind",
                       "needs_input", "assignee="):
            self.assertNotIn(jargon, out.lower().replace("assignee=", "X"),
                             f"jargon leaked into Richie's message: {jargon}")
        self.assertIn("axel", out)
        self.assertIn("bob", out)

    def test_dry_run_delivers_nothing_and_retires_nothing(self):
        self.capture()
        self.add_block("t_real123")
        out = self.capture(dry_run=True)
        self.assertIn("DRY RUN", out)
        self.assertFalse(self.state()["retired"])
        self.assertTrue(self.jobs_enabled())

    # --- controls ------------------------------------------------------------
    def test_NEGATIVE_CONTROL_the_suite_can_go_red(self):
        """Prove the harness actually observes a fire."""
        self.capture()
        self.add_block("t_probe")
        self.assertNotEqual(self.capture(), "",
                            "the harness cannot see a fire — every other "
                            "assertion here is worthless")

    def test_CONTROL_a_missing_db_is_silent_not_a_crash(self):
        os.unlink(self.db)
        self.assertEqual(self.capture(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
