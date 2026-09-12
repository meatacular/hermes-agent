#!/usr/bin/env python3
"""First-fire watch — tell Richie the FIRST time the routing auditor blocks a real card.

Why this exists
---------------
`assignee-mismatch-watch --apply` was armed on 2026-09-13 after being proved on
PLANTED cards. Planted cards are shaped the way the author expected, because the
author shaped them. The auditor has never acted on a card it did not receive from
a test. The two unproven risks are that it misses a real mis-routing, or that it
blocks a card it should not — and the second one costs Richie, because a wrongly
blocked card is work that silently stops.

So: the first time the auditor blocks a card that nobody planted, say so in plain
English, once, and then retire. This is a bridge over one gap, not a permanent
watchdog. After it fires it disables its own cron entry.

Silent on every run except that one. Zero tokens (`no_agent`). Stdlib only.

Delivery: prints -> the cron `deliver` target (photon iMessage), plus a Slack DM
via fleet_notify, matching escalation-watch.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent

KANBAN_DB = HERMES_HOME / "kanban.db"
STATE = HERMES_HOME / "state" / "first-fire-watch.json"
JOBS = HERMES_HOME / "cron" / "jobs.json"
JOB_NAME = "first-fire-watch"

# Ids used by this session's own live proofs. A watchdog that congratulates
# itself on its author's fixtures is worth nothing.
TEST_PREFIXES = ("t_amwtest", "t_amwcap", "t_ffwtest")

AUDITOR = "assignee-mismatch-watch"


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"retired": False, "baseline_event_id": None}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2))


def is_test_card(task_id):
    return any(str(task_id).startswith(p) for p in TEST_PREFIXES)


def find_first_real_block(con, after_event_id):
    """Earliest auditor block on a non-test card after the baseline event id."""
    rows = con.execute(
        "SELECT e.id, e.task_id, e.created_at, t.title, t.assignee, e.payload "
        "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.kind='blocked' AND e.payload LIKE ? AND e.id > ? "
        "ORDER BY e.id LIMIT 50",
        (f"%{AUDITOR}%", after_event_id or 0),
    ).fetchall()
    for r in rows:
        if not is_test_card(r["task_id"]):
            return r
    return None


def plain_english(row):
    """What Richie reads. State, the decision, nothing else.

    No card ids as the subject, no charter references, no lane jargon in the
    lead. The card id goes last, for when he wants to find it.
    """
    try:
        pl = json.loads(row["payload"] or "{}")
    except Exception:
        pl = {}
    reason = str(pl.get("reason") or "")
    expected = actual = ""
    # reason looks like: "mint assignee=axel (payload) on build-lane card; expected bob"
    if "assignee=" in reason:
        actual = reason.split("assignee=", 1)[1].split()[0].strip("(),")
    if "expected " in reason:
        expected = reason.rsplit("expected ", 1)[1].strip().strip(".")
    title = (row["title"] or "").strip() or "(untitled card)"

    who = ""
    if expected and actual:
        who = f"It was given to {actual}; it looks like {expected}'s work.\n"
    return (
        "The routing checker stopped a job for the first time.\n\n"
        f"Job: {title}\n"
        f"{who}"
        "\nNothing is lost — it's paused, waiting to be pointed at the right "
        "person. If that call looks wrong, tell me and I'll switch the checker "
        "back to watching only.\n\n"
        "This is the first one, so it's worth a look. I won't message you about "
        "the rest.\n"
        f"\n(card {row['task_id']})"
    )


def retire_cron():
    """Best-effort: disable this watchdog's own cron entry, and VERIFY it took.

    `cron/jobs.json` is owned by the scheduler, which rewrites it to record
    run state. A write from inside a job can therefore be clobbered -- it was,
    on the 2026-09-13 delivery proof: the state file said retired, the cron
    entry was still enabled, and nothing said so. The hermetic test passed
    because no scheduler was racing it.

    So this is explicitly NOT the retirement mechanism. `state.retired` is, and
    it is checked first thing in run(). This just tidies up, and reports
    honestly whether it managed to. Never raises.
    """
    try:
        d = json.loads(JOBS.read_text())
        jobs = d["jobs"] if isinstance(d, dict) and "jobs" in d else d
        hit = False
        for j in jobs:
            if j.get("name") == JOB_NAME:
                j["enabled"] = False
                j["paused_reason"] = "fired once and retired (first-fire-watch)"
                hit = True
        if not hit:
            return False
        JOBS.write_text(json.dumps(d, indent=2))
        # Read it back. A write that was not persisted is not a retirement.
        d2 = json.loads(JOBS.read_text())
        jobs2 = d2["jobs"] if isinstance(d2, dict) and "jobs" in d2 else d2
        return all(j.get("enabled") is False
                   for j in jobs2 if j.get("name") == JOB_NAME)
    except Exception:
        return False


def run(args):
    st = load_state()
    if st.get("retired") and not args.force:
        return 0

    if not KANBAN_DB.exists():
        return 0
    con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        if st.get("baseline_event_id") is None:
            # First ever run: remember where history ends, say nothing. Existing
            # blocks are not news.
            row = con.execute(
                "SELECT COALESCE(MAX(id), 0) m FROM task_events").fetchone()
            st["baseline_event_id"] = row["m"]
            save_state(st)
            if args.verbose:
                print(f"baselined at task_events.id={row['m']}")
            return 0
        hit = find_first_real_block(con, st["baseline_event_id"])
    finally:
        con.close()

    if not hit:
        return 0

    msg = plain_english(hit)
    if args.dry_run:
        print("--- DRY RUN, not delivered ---")
        print(msg)
        return 0

    print(msg)                      # -> cron deliver -> iMessage
    try:
        sys.path.insert(0, str(HERMES_HOME / "scripts"))
        import fleet_notify
        fleet_notify.slack_dm(msg)
    except Exception:
        pass                        # a notifier that crashes the watchdog is worse

    # The state file IS the retirement. Write it before touching anything else,
    # so a crash in the tidy-up can never cause a second message.
    st["retired"] = True
    st["fired_at"] = int(time.time())
    st["fired_on"] = hit["task_id"]
    save_state(st)
    st["cron_retired"] = retire_cron()
    save_state(st)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent; deliver nothing, retire nothing")
    ap.add_argument("--force", action="store_true",
                    help="run even if already retired (testing only)")
    ap.add_argument("--verbose", action="store_true")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
