#!/usr/bin/env python3
"""Executable tests for release-operator-hold-watch — the operator_hold guard rail.

Zero-LLM, stdlib-only, black-box (the real script, run as cron runs it):

    python3 test_release_operator_hold_watch.py

Point the harness at a different copy with ``RELEASE_HOLD_WATCH_SCRIPT=/path/to/script.py``; that
is how a red-before/green-after proof is taken.

**The contract changed on 2026-09-15 and this suite changed with it.** Richie: "I don't want any
mandatory holds added back to the system ... I don't want work to continue to stop and wait on my
approval apart from design review or cost cap or turn reviews." So the polarity inverted: a hold
now needs a REASON to survive, where before a release needed a reason to happen. The suite was left
pinning the old polarity for a few hours and went red on correct behaviour — which is how a stale
test suite on a safety watchdog turns into noise. What it pins now:

1. Design approval (karl / ``[Karl]``) is still held — exemption one of the two Richie kept.
2. An explicit, line-anchored ``operator-hold: manual`` is still held — exemption two. Prose that
   merely mentions the marker mid-sentence is NOT a marker and does not create a gate.
3. Everything else releases: a standalone hold (nothing to wait for), and a card whose dependency
   parents are all done, with or without a ``dependency-wait`` marker.
4. A card still waiting on a parent is held, silently — a routine tick says nothing.
5. **Release once.** A card this watchdog released that comes back blocked within the window is
   held under ``re_blocked`` and announced once, instead of being released into the same failure
   again. (t_796c3fab, 2026-09-15: released 06:14, worker blocked 06:35, overwatch re-held 06:37.)
6. Every unblock carries ``--reason``, so the board records which rule released the card and who
   did it — overwatch could not tell, because the bare unblock wrote an event with payload=None.
7. Every emitted line names its rule, and the state file stays append-only and parseable with a
   rule on every entry.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("RELEASE_HOLD_WATCH_SCRIPT") or os.path.join(
    _HERE, "release-operator-hold-watch.py")

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append((name, got, want))
    return ok


def check_true(name, cond, detail=""):
    ok = bool(cond)
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if not ok and detail else ""))
    if not ok:
        FAILURES.append((name, bool(cond), True))
    return ok


SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
    status TEXT, block_kind TEXT
);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT
);
"""


class Rig:
    """One isolated HERMES_HOME: its own board, its own state, its own
    `hermes` binary that records what the watchdog tried to unblock."""

    def __init__(self):
        import sqlite3
        self.tmp = tempfile.mkdtemp(prefix="release-hold-test-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "state"))
        self.db = os.path.join(self.home, "kanban.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self.record = os.path.join(self.tmp, "unblock.calls")
        hermes = os.path.join(self.home, "hermes-agent", "venv", "bin", "hermes")
        os.makedirs(os.path.dirname(hermes))
        with open(hermes, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UNBLOCK_RECORD"\nexit 0\n')
        os.chmod(hermes, os.stat(hermes).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        self._sql("INSERT INTO task_events (task_id, kind, payload) VALUES (?,?,?)",
                  ("_seed", "created", "{}"))

    def _sql(self, sql, params=()):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def card(self, tid, title="card", assignee="bob", body="",
             status="blocked", block_kind="operator_hold"):
        self._sql(
            "INSERT INTO tasks (id, title, body, assignee, status, block_kind) "
            "VALUES (?,?,?,?,?,?)",
            (tid, title, body, assignee, status, block_kind))

    def parent(self, tid, status="done"):
        self._sql("INSERT INTO tasks (id, title, assignee, status, block_kind) "
                  "VALUES (?,?,?,?,NULL)", (tid, "parent", "bob", status))

    def link(self, child, parent):
        self._sql("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)",
                  (parent, child))

    def block_reason(self, tid, reason):
        self._sql("INSERT INTO task_events (task_id, kind, payload) VALUES (?,?,?)",
                  (tid, "blocked", json.dumps({"reason": reason, "kind": "operator_hold"})))

    def run(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("RELEASE_HOLD_DB", "RELEASE_HOLD_STATE",
                            "RELEASE_HOLD_DRYRUN", "UNBLOCK_RECORD")}
        env["HERMES_HOME"] = self.home
        env["UNBLOCK_RECORD"] = self.record
        proc = subprocess.run([sys.executable, SCRIPT], capture_output=True,
                              text=True, env=env, timeout=60)
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")

    def calls(self):
        if not os.path.exists(self.record):
            return []
        with open(self.record) as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def state(self):
        path = os.path.join(self.home, "state", "release-operator-hold-watch.json")
        with open(path) as fh:
            return json.load(fh)

    def skipped_rule(self, last=-1):
        run = self.state()[last]
        # "" for a missing rule, so a pre-change script reports rather than crashes
        return [str(s.get("rule") or "") for s in run.get("skipped", [])]

    def released_ids(self, last=-1):
        return [r["id"] for r in self.state()[last].get("released", [])]


UNBLOCK = "kanban unblock --reason release-operator-hold-watch"


def _reasoned(rule, tid):
    """The exact call shape a release must make: rule-tagged --reason, then the id."""
    return lambda call: call.startswith(f"{UNBLOCK} [{rule}]:") and call.endswith(f" {tid}")


def case_design_is_held():
    """Exemption 1 — design approval survives the no-mandatory-holds rule."""
    print("\n1. design approval is still held (assignee and title prefix)")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_karl", title="[Karl] WP7 design", assignee="karl")
    rig.link("t_karl", "t_parent")
    rig.card("t_karl_title", title="[Karl] WP10 design", assignee="jobsy")
    rig.link("t_karl_title", "t_parent")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("no unblock attempted", rig.calls(), [])
    check("nothing released", rig.released_ids(), [])
    check("both held as design_approval", sorted(rig.skipped_rule()),
          ["design_approval", "design_approval"])


def case_manual_marker_is_held():
    """Exemption 2 — the card says `operator-hold: manual` in so many words."""
    print("\n2. explicit `operator-hold: manual` is still held")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_manual", title="explicit human gate", body="operator-hold: manual\n")
    rig.link("t_manual", "t_parent")
    rig.card("t_both", title="both markers",
             body="operator-hold: dependency-wait\noperator-hold: manual\n")
    rig.link("t_both", "t_parent")
    rig.card("t_lone_manual", title="standalone but explicitly manual",
             body="**operator-hold: manual**\n")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("no unblock attempted", rig.calls(), [])
    rules = {x["id"]: x.get("rule") for x in rig.state()[-1]["skipped"]}
    check("manual held", rules.get("t_manual"), "manual_hold")
    check("manual beats dependency-wait", rules.get("t_both"), "manual_hold")
    check("manual beats the standalone release", rules.get("t_lone_manual"), "manual_hold")


def case_everything_else_releases():
    """The 2026-09-15 policy: a hold needs a reason to survive, not the other way round."""
    print("\n3. standalone holds and parents-done cards are released")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_lone", title="standalone hold")
    rig.card("t_marked", title="declared dependency wait",
             body="operator-hold: dependency-wait\n")
    rig.link("t_marked", "t_parent")
    rig.card("t_unmarked", title="no marker, parents done", body="just a brief\n")
    rig.link("t_unmarked", "t_parent")
    rig.card("t_prose", title="prose mention is not a marker",
             body="Do NOT add `operator-hold: manual` to this card.\n")
    rig.link("t_prose", "t_parent")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("all four released", sorted(rig.released_ids()),
          ["t_lone", "t_marked", "t_prose", "t_unmarked"])
    rules = {r["id"]: r.get("rule") for r in rig.state()[-1]["released"]}
    check("standalone rule", rules.get("t_lone"), "no_parents")
    check("declared dependency-wait rule", rules.get("t_marked"), "dependency_wait")
    check("no-marker rule", rules.get("t_unmarked"), "no_marker")
    check("prose mention did NOT create a manual gate", rules.get("t_prose"), "no_marker")
    check_true("every line names its rule",
               all(("RELEASE [" in ln) for ln in out.splitlines()
                   if ln.startswith(("RELEASE", "HOLD", "FAILED"))), out)


def case_reason_is_recorded():
    """Every unblock carries --reason, so the board records WHY, not just that it happened."""
    print("\n4. the unblock records the rule and reason on the card")
    rig = Rig()
    rig.card("t_lone", title="standalone hold")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("exactly one unblock", len(rig.calls()), 1)
    call = rig.calls()[0]
    check_true("call carries --reason, the rule, and the id last",
               _reasoned("no_parents", "t_lone")(call), call)
    check_true("the reason names the watchdog", "release-operator-hold-watch" in call, call)
    check_true("the reason is human-readable", "standalone hold" in call, call)


def case_pending_is_silent():
    """A card still waiting on a parent is held, and the tick says nothing."""
    print("\n5. a routine tick is silent; a material one speaks (AC3)")
    rig = Rig()
    rig.parent("t_parent", "todo")
    rig.card("t_wait", title="waiting", body="operator-hold: dependency-wait\n")
    rig.link("t_wait", "t_parent")
    rig.card("t_design", title="[Karl] design", assignee="karl")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("silent on a routine tick", out, "")
    check("rules recorded", sorted(rig.skipped_rule()),
          ["design_approval", "parents_pending"])
    check("nothing unblocked", rig.calls(), [])

    import sqlite3
    conn = sqlite3.connect(rig.db)
    conn.execute("UPDATE tasks SET status='done' WHERE id='t_parent'")
    conn.commit()
    conn.close()
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check_true("release line names the rule", "RELEASE [dependency_wait] t_wait" in out, out)
    check_true("unblocked after the parent completed",
               any(_reasoned("dependency_wait", "t_wait")(c) for c in rig.calls()), rig.calls())


def case_release_once():
    """The loop breaker: released, came back blocked, do NOT release again."""
    print("\n6. a card that comes back blocked is held under re_blocked, announced once")
    rig = Rig()
    rig.card("t_lone", title="standalone hold")
    _, out1, _ = rig.run()
    check_true("first tick releases", "RELEASE [no_parents] t_lone" in out1, out1)
    check("released once", len(rig.calls()), 1)

    # the worker hit it and blocked; overwatch re-held it — the exact t_796c3fab shape
    _, out2, _ = rig.run()
    check_true("second tick holds it", "HOLD [re_blocked] t_lone" in out2, out2)
    check("still only one unblock ever attempted", len(rig.calls()), 1)
    check("held under re_blocked", rig.skipped_rule(), ["re_blocked"])

    _, out3, _ = rig.run()
    check("third tick is silent — announced once, not every tick", out3, "")
    state = rig.state()
    check("all three runs appended", len(state), 3)
    check("announced exactly once",
          sum(len(r.get("announced", [])) for r in state), 1)
    check("run keys", sorted(state[-1].keys()),
          ["announced", "released", "run_at", "skipped"])
    check_true("every entry carries a rule",
               all(e.get("rule") for run in state
                   for b in ("released", "skipped", "announced")
                   for e in run.get(b, [])),
               json.dumps(state[-1])[:400])


def case_design_beats_reblock():
    """Ordering: the two exemptions Richie kept are checked BEFORE the loop breaker, so a design
    card is reported as design_approval — the reason he would act on — not as re_blocked."""
    print("\n7. exemptions are reported ahead of the loop breaker")
    rig = Rig()
    rig.card("t_k", title="[Karl] design", assignee="karl")
    rig.run()
    check("design card was never released", rig.released_ids(), [])
    check("and is reported as design_approval, not re_blocked",
          rig.skipped_rule(), ["design_approval"])


def main():
    print(f"release-operator-hold-watch tests — script under test:\n  {SCRIPT}")
    if not os.path.exists(SCRIPT):
        print(f"FAIL: script not found: {SCRIPT}")
        return 2
    case_design_is_held()
    case_manual_marker_is_held()
    case_everything_else_releases()
    case_reason_is_recorded()
    case_pending_is_silent()
    case_release_once()
    case_design_beats_reblock()
    print(f"\n{'FAILED' if FAILURES else 'ALL PASS'} — {len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
