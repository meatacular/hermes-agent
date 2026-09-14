#!/usr/bin/env python3
"""Executable tests for release-operator-hold-watch — the operator_hold guard rail.

Zero-LLM, stdlib-only, black-box (the real script, run as cron runs it):

    python3 test_release_operator_hold_watch.py

Point the harness at a different copy of the script with
``RELEASE_HOLD_WATCH_SCRIPT=/path/to/script.py`` — that is how the
red-before/green-after proof for card t_aa3bcf6c was taken:

    RELEASE_HOLD_WATCH_SCRIPT=release-operator-hold-watch.py.bak-release-operator-hold \
        python3 test_release_operator_hold_watch.py     # RED (4 failures)
    python3 test_release_operator_hold_watch.py         # GREEN

What it pins:
1. A card minted `blocked` + `operator_hold` whose gate is a human approval is
   NOT released when its dependency parents complete (AC1) — including the
   kernel's own deploy-follow-up shape (`_maybe_create_deploy_followup`), whose
   body says "Do not unblock it yourself".
2. A card that declares itself a dependency wait *is* still released, so
   Richie's 2026-09-14 policy survives for that class (AC2).
3. The existing exemptions are not weakened: design cards (karl / [Karl]) and
   standalone holds.
4. A marker mentioned in prose does not arm a release, and `manual` beats
   `dependency-wait`.
5. Every emitted line names the rule that released or skipped the card (AC3),
   a held-and-waiting card is announced once rather than every tick, and a
   routine tick is silent.
6. The state file stays append-only and parseable, every entry carrying its
   rule (AC4).
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


def case_human_gate_is_held():
    """AC1 — the deploy-card shape: held for Richie's approval, parent done."""
    print("\n1. human-gate card with parents done is NOT released (AC1)")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_deploy",
             title="Deploy: merge wt/x + restart gateway",
             body="**This card is HELD (`operator_hold`).** Charter §8: approval "
                  "precedes deploy. Richie releases it. Do not unblock it "
                  "yourself.\n")
    rig.link("t_deploy", "t_parent")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("no unblock attempted", rig.calls(), [])
    check("nothing released", rig.released_ids(), [])
    check("recorded as no_marker", rig.skipped_rule(), ["no_marker"])
    check_true("line names the rule and the card",
               "HELD [no_marker] t_deploy" in out, out)


def case_marker_still_releases():
    """AC2 — a declared dependency wait is still released (2026-09-14 policy)."""
    print("\n2. card that declares a dependency wait still releases (AC2)")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_marked", title="WP7 — Digest surface",
             body="## Brief\nBuild the digest.\n\noperator-hold: dependency-wait\n")
    rig.link("t_marked", "t_parent")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("unblock attempted once", rig.calls(), ["kanban unblock t_marked"])
    check("released", rig.released_ids(), ["t_marked"])
    check("release rule recorded",
          rig.state()[-1]["released"][0].get("rule"), "dependency_wait")
    check_true("line names the rule", "RELEASE [dependency_wait] t_marked" in out, out)


def case_exemptions_hold():
    """AC — the design and no-parent exemptions are not weakened."""
    print("\n3. design cards and standalone holds stay held")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_karl", title="[Karl] WP7 design", assignee="karl",
             body="operator-hold: dependency-wait\n")
    rig.link("t_karl", "t_parent")
    rig.card("t_karl_title", title="[Karl] WP10 design", assignee="jobsy",
             body="operator-hold: dependency-wait\n")
    rig.link("t_karl_title", "t_parent")
    rig.card("t_lone", title="[cut fidelity] standalone hold")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("no unblock attempted", rig.calls(), [])
    check("nothing released", rig.released_ids(), [])
    check("rules recorded", rig.skipped_rule(),
          ["design_approval", "design_approval", "no_parents"])


def case_marker_hygiene():
    """A marker is a declaration: prose mentions and manual must not release."""
    print("\n4. marker hygiene — prose is not a marker, manual wins")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_prose", title="prose mention",
             body="Do NOT add `operator-hold: dependency-wait` to this card.\n")
    rig.link("t_prose", "t_parent")
    rig.card("t_manual", title="explicit human gate",
             body="operator-hold: manual\n")
    rig.link("t_manual", "t_parent")
    rig.card("t_both", title="both markers",
             body="operator-hold: dependency-wait\noperator-hold: manual\n")
    rig.link("t_both", "t_parent")
    rig.card("t_reason", title="marker in the block reason",
             body="no marker in the body\n")
    rig.link("t_reason", "t_parent")
    rig.block_reason("t_reason", "waiting on nothing but the parent task.\n"
                                 "operator-hold: dependency-wait\n")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("only the reason-marked card unblocked",
          rig.calls(), ["kanban unblock t_reason"])
    check("released set", rig.released_ids(), ["t_reason"])
    rules = {s["id"]: s.get("rule") for s in rig.state()[-1]["skipped"]}
    check("prose mention held", rules.get("t_prose"), "no_marker")
    check("manual held", rules.get("t_manual"), "manual_hold")
    check("manual beats dependency-wait", rules.get("t_both"), "manual_hold")


def case_pending_and_silence():
    """Routine ticks are silent; a release/held-gate tick is not."""
    print("\n5. routine ticks are silent, material ticks speak with rules (AC3)")
    rig = Rig()
    rig.parent("t_parent", "todo")
    rig.card("t_wait", title="waiting", body="operator-hold: dependency-wait\n")
    rig.link("t_wait", "t_parent")
    rig.card("t_design", title="[Karl] design", assignee="karl")
    rig.card("t_lone", title="standalone")
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("silent on a routine tick", out, "")
    check("rules recorded", sorted(rig.skipped_rule()),
          ["design_approval", "no_parents", "parents_pending"])
    check("nothing unblocked", rig.calls(), [])

    # The waiting card's parent completes: now it is material (released).
    rig.parent  # noqa: B018 — the parent row already exists
    import sqlite3
    conn = sqlite3.connect(rig.db)
    conn.execute("UPDATE tasks SET status='done' WHERE id='t_parent'")
    conn.commit()
    conn.close()
    rc, out, err = rig.run()
    check("exit code", rc, 0)
    check("release line names the rule",
          "RELEASE [dependency_wait] t_wait" in out, True)
    check("unblocked after the parent completed",
          rig.calls(), ["kanban unblock t_wait"])


def case_announce_once_and_state():
    """AC3/AC4 — announce-once, and the state file stays append-only."""
    print("\n6. held-and-waiting is announced once; state is append-only (AC4)")
    rig = Rig()
    rig.parent("t_parent", "done")
    rig.card("t_armed", title="deploy awaiting approval",
             body="Do not unblock it yourself.\n")
    rig.link("t_armed", "t_parent")
    _, out1, _ = rig.run()
    check_true("first tick announces", "HELD [no_marker] t_armed" in out1, out1)
    _, out2, _ = rig.run()
    check("second tick is silent", out2, "")
    state = rig.state()
    check("both runs appended", len(state), 2)
    check("announced once", sum(len(r.get("announced", [])) for r in state), 1)
    check("run keys", sorted(state[-1].keys()),
          ["announced", "released", "run_at", "skipped"])
    check_true("every entry carries a rule",
               all(e.get("rule") for run in state
                   for b in ("released", "skipped", "announced")
                   for e in run.get(b, [])),
               json.dumps(state[-1])[:400])


def main():
    print(f"release-operator-hold-watch tests — script under test:\n  {SCRIPT}")
    if not os.path.exists(SCRIPT):
        print(f"FAIL: script not found: {SCRIPT}")
        return 2
    case_human_gate_is_held()
    case_marker_still_releases()
    case_exemptions_hold()
    case_marker_hygiene()
    case_pending_and_silence()
    case_announce_once_and_state()
    print(f"\n{'FAILED' if FAILURES else 'ALL PASS'} — "
          f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
