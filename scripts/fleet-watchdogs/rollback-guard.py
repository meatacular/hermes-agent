#!/usr/bin/env python3
"""rollback-guard — bounded automatic rollback of a bad update (2026-09-03).

Charter §12 says rollback is a recommendation; Richie asked for the narrow,
safe automation after the break test: IF the dispatcher has thrown on ≥3 ticks
in the last 10 minutes AND a manifest carrying a `git` item was applied within the
last 60 minutes AND it has not already been rolled back — roll THAT manifest
back (git revert + db restore + three restarts) and tell Richie. Nothing else
is ever rolled back automatically; a break with no recent update is reported
only (pre-flight check 9 / this watch's message), because reverting an old
change would not fix it.

Root cron, every 5 min, zero tokens. State: state/rollback-guard.json.
Test overrides: RG_LOGS (colon-separated), RG_CHANGES_DIR, RG_DRYRUN=1,
RG_NOW (epoch), RG_WINDOW_MIN.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from fleet_notify import slack_dm
except Exception:  # noqa: BLE001
    def slack_dm(text, channel=None):  # type: ignore
        return False

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
CHANGES = Path(os.environ.get("RG_CHANGES_DIR") or HERMES_HOME / "changes")
STATE = HERMES_HOME / "state" / "rollback-guard.json"
LOGS = [Path(p) for p in os.environ["RG_LOGS"].split(":")] if os.environ.get("RG_LOGS") else [
    HERMES_HOME / "logs" / "gateway.log", HERMES_HOME / "profiles" / "axel" / "logs" / "gateway.log",
    HERMES_HOME / "profiles" / "switch" / "logs" / "gateway.log"]
NOW = float(os.environ.get("RG_NOW") or time.time())
DRY = bool(os.environ.get("RG_DRYRUN"))
FAIL_MIN = 3
FAIL_WINDOW_S = 10 * 60
APPLY_WINDOW_S = int(os.environ.get("RG_WINDOW_MIN") or 60) * 60


def tick_failures():
    cutoff = NOW - FAIL_WINDOW_S
    n = 0
    for lg in LOGS:
        try:
            tail = lg.read_bytes()[-400_000:].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        for line in tail.splitlines():
            if "kanban dispatcher: tick failed" in line:
                m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if m and time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) >= cutoff:
                    n += 1
    return n


SCHEMA_FILE_HINTS = ("schema", "migration", "migrate")


def _touches_schema(git_item):
    """Did this manifest's git range touch anything schema-shaped?

    Fails CLOSED toward inaction: if git cannot answer, we say yes, which makes
    the manifest ineligible for automatic rollback. Here that is the safe
    direction — the cost of not auto-reverting is a report Richie reads, and the
    cost of auto-reverting a migration is a database that no longer matches its
    code. Keep the hints in step with `upstream-update-watch.SCHEMA_FILE_HINTS`.
    """
    repo, pre, post = git_item.get("repo"), git_item.get("pre"), git_item.get("post")
    if not (repo and pre and post):
        return True
    try:
        p = subprocess.run(["git", "diff", "--name-only", f"{pre}..{post}"], cwd=repo,
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return True
    except Exception:  # noqa: BLE001
        return True
    return any(any(h in f.lower() for h in SCHEMA_FILE_HINTS) and f.endswith((".py", ".sql"))
               for f in p.stdout.splitlines())


def recent_update_manifest():
    """The newest applied manifest in the window that could plausibly have
    broken the dispatcher.

    2026-09-07: this used to glob `upstream-*.json` only, so `deploy-20260907pm`
    — a hand-run fast-forward of the platform repo that restarted all three
    gateways — was completely invisible to this guard, as was every other
    hand-run deploy. An automated merge got a safety net that an attended one
    did not, which is backwards: the attended path is the one a tired human
    drives at 18:45.

    The selector is now SEMANTIC rather than a naming convention: any manifest
    carrying a `git` item, because a change to platform code is the only kind
    that can make the gateway's dispatcher throw. A files-only manifest editing
    `~/.hermes/scripts/*.py` cannot — those are `no_agent` cron scripts spawned
    fresh each tick, outside the gateway process entirely. Matching on an id
    prefix would have meant `ceilinggap-20260907` (three script files, no git)
    was eligible for an automatic revert while `deploy-20260907pm` was not.

    Naming conventions are exactly what this fleet keeps getting caught by
    (trap 14, trap 17, `run-cache-discount-watch.py` vs `cache-discount-watch.py`).
    Ask what the manifest DID, not what it was called.
    """
    from datetime import datetime
    best = None
    for p in CHANGES.glob("*.json"):
        try:
            m = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            continue
        if m.get("status") != "applied" or not m.get("applied_at"):
            continue
        if m.get("rolled_back_at"):
            continue
        gits = [it for it in (m.get("items") or []) if it.get("kind") == "git"]
        if not gits:
            continue  # no platform code changed — reverting it would not help
        if any(_touches_schema(g) for g in gits):
            # A revert cannot un-migrate a database. For an `upstream-*` manifest
            # this could not arise (schema changes are Tier B by construction and
            # never auto-applied), but widening the net to hand-run deploys means
            # a hand-run migration is now reachable — and reverting the code while
            # leaving the schema migrated is worse than doing nothing.
            print(f"note: {m['id']} touches schema/migration files — not eligible "
                  f"for automatic rollback, reporting only")
            continue
        try:
            ts = datetime.fromisoformat(m["applied_at"]).timestamp()
        except Exception:  # noqa: BLE001
            continue
        if NOW - ts <= APPLY_WINDOW_S and (best is None or ts > best[1]):
            best = (m["id"], ts)
    return best


def main():
    n = tick_failures()
    if n < FAIL_MIN:
        return 0
    try:
        st = json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception:  # noqa: BLE001
        st = {}
    cand = recent_update_manifest()
    if not cand:
        key = f"nomanifest:{int(NOW // 1800)}"
        if st.get("last") != key:
            msg = (f"*Dispatcher throwing* — {n} `tick failed` in 10 min, but no code-changing manifest applied in the last "
                   f"{APPLY_WINDOW_S // 60} min, so nothing is rolled back automatically. Check the gateway logs; "
                   f"`change-manifest.py list` shows what changed last.")
            print(msg); slack_dm(msg)
            st["last"] = key; STATE.parent.mkdir(parents=True, exist_ok=True); STATE.write_text(json.dumps(st))
        return 0
    cid, ts = cand
    if st.get("rolled_back") == cid:
        return 0
    if DRY:
        print(f"(dry) would roll back {cid}: {n} tick failures, applied {(NOW - ts) / 60:.0f} min ago")
        return 0
    # No --with-db, and now it is ENFORCED rather than assumed. The old reasoning was
    # "a Tier-A merge never migrates a schema (schema changes are Tier B by construction)",
    # which was true but load-bearing on a naming convention. Since this guard now covers
    # hand-run deploys too, `_touches_schema` filters any migrating manifest out of the
    # candidate set above, so a code revert + restarts really is the whole fix here and no
    # board writes are lost.
    r = subprocess.run(["/bin/bash", str(HERMES_HOME / "scripts" / "fleet-rollback.sh"), cid],
                       capture_output=True, text=True, timeout=900)
    tail = " | ".join((r.stdout or r.stderr).strip().splitlines()[-3:])
    try:
        (HERMES_HOME / "logs").mkdir(exist_ok=True)
        with (HERMES_HOME / "logs" / "rollback-guard.jsonl").open("a") as f:
            f.write(json.dumps({"at": int(NOW), "failures": n, "manifest": cid, "applied_age_s": int(NOW - ts), "rc": r.returncode}) + "\n")
    except Exception:  # noqa: BLE001
        pass
    msg = (f"🛑 *Auto-rollback*: dispatcher threw {n}× in 10 min, {(NOW - ts) / 60:.0f} min after update manifest `{cid}` "
           f"was applied. Rolled it back (code revert + root/axel/switch restart; db snapshot kept, not restored): {tail}. "
           f"The upstream tip stays reported as pending; take it attended.")
    print(msg); slack_dm(msg)
    st["rolled_back"] = cid; STATE.parent.mkdir(parents=True, exist_ok=True); STATE.write_text(json.dumps(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
