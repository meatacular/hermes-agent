#!/usr/bin/env python3
"""fleet-integrity-watch — silent unless the fleet's own code has been undone.

Written 2026-09-02 after the auto-updater's `git reset --hard origin/main` at
06:53:28 erased 59 fleet commits and reverted the running gateway to stock
upstream. Nothing in the fleet noticed for two and a half hours; the docs still
described eleven fixes as "deployed".

Four assertions, no LLM, no network. Empty stdout = healthy tick.

  1. HEAD is on the parked branch (a silent un-park means the NEXT update resets)
  2. sentinel fleet commits are still ancestors of HEAD (the reset itself)
  3. the working tree is clean (a dirty tree makes the next update SKIP)
  4. the dispatcher heartbeat is fresh (the board is actually being served)

(1)-(3) are exactly the three ways the parked-branch arrangement can fail
quietly; (4) is the freeze that started all of this.
"""
import json
import os
import subprocess
import sys
import time

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
REPO = os.path.join(HERMES_HOME, "hermes-agent")
PARKED_BRANCH = "fleet"
HEARTBEAT = os.path.join(HERMES_HOME, "kanban", ".dispatcher.heartbeat")
HEARTBEAT_STALE_SECONDS = 900          # 15 min; dispatcher ticks every 60s
STATE = os.path.join(HERMES_HOME, "state", "fleet-integrity-watch.json")

# Sentinel commits — one per load-bearing subsystem from the 09-01/09-02
# hardening programme. If any is not an ancestor of HEAD, the fleet's code has
# been undone. Add to this list as new load-bearing work lands.
SENTINELS = {
    "d3466a7273": "finalize guard",
    "154fd15f93": "failure-class split",
    "964ed16dc4": "review ladder",
    "6334b07c48": "cost cap (kanban_create path)",
    "e20b340bb8": "dispatcher heartbeat + takeover",
    "c5bc7b14db": "auto-decomposer guards",
    "08ef937e74": "falsifiable first-pass metric",
}


def git(*args, timeout=20):
    try:
        p = subprocess.run(["git"] + list(args), cwd=REPO, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:                       # pragma: no cover
        return 1, "git failed: %s" % exc


def main():
    problems = []

    if not os.path.isdir(os.path.join(REPO, ".git")):
        print("CRITICAL: %s is not a git repo — cannot verify fleet code." % REPO)
        return 1

    rc, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        problems.append("cannot read current branch: %s" % branch)
    elif branch != PARKED_BRANCH:
        problems.append(
            "HEAD is on '%s', expected the parked branch '%s'. The checkout has been\n"
            "  un-parked — the NEXT `hermes update` will `git reset --hard origin/main`\n"
            "  and discard fleet work. Fix: git checkout %s"
            % (branch, PARKED_BRANCH, PARKED_BRANCH))

    missing = []
    for sha, what in SENTINELS.items():
        rc, _ = git("merge-base", "--is-ancestor", sha, "HEAD")
        if rc != 0:
            missing.append("%s (%s)" % (sha, what))
    if missing:
        problems.append(
            "%d fleet commit(s) are NO LONGER in the running code — the tree has been\n"
            "  reset or rewritten:\n    %s\n"
            "  Recovery: git checkout %s (commits survive in the reflog and wt/* branches)."
            % (len(missing), "\n    ".join(missing), PARKED_BRANCH))

    rc, dirty = git("status", "--porcelain")
    if rc == 0 and dirty:
        n = len(dirty.splitlines())
        problems.append(
            "working tree is DIRTY (%d file(s)). `hermes update` refuses to touch a dirty\n"
            "  parked branch, so the next update will be SKIPPED and the fleet will silently\n"
            "  fall behind upstream. Commit or restore:\n    %s"
            % (n, "\n    ".join(dirty.splitlines()[:8])))

    try:
        age = time.time() - os.path.getmtime(HEARTBEAT)
        if age > HEARTBEAT_STALE_SECONDS:
            problems.append(
                "dispatcher heartbeat is STALE (%dm old, window %dm) — the board is probably\n"
                "  frozen. Check which gateway holds kanban/.dispatcher.lock."
                % (age // 60, HEARTBEAT_STALE_SECONDS // 60))
    except OSError:
        problems.append(
            "dispatcher heartbeat file is ABSENT (%s). Either no gateway owns the dispatcher,\n"
            "  or the heartbeat writer has been reverted — in which case kanban-liveness-watch\n"
            "  is also silently disarmed." % HEARTBEAT)

    if not problems:
        _save({"ok": True, "at": time.time(), "head": git("rev-parse", "--short", "HEAD")[1]})
        return 0                                   # silent tick

    # De-duplicate: only re-alert when the problem set changes, or every 6h.
    sig = "|".join(p.split("\n")[0] for p in problems)
    prev = _load()
    if prev.get("sig") == sig and (time.time() - prev.get("at", 0)) < 6 * 3600:
        return 0
    _save({"sig": sig, "at": time.time(), "ok": False})

    print("FLEET INTEGRITY ALERT — the fleet's own code or dispatcher is not as expected.")
    print("repo: %s   branch: %s" % (REPO, branch))
    print()
    for i, p in enumerate(problems, 1):
        print("%d. %s" % (i, p))
        print()
    print("Context: ~/.hermes/hermes-agent tracks NousResearch upstream. `hermes update`")
    print("does `git reset --hard origin/main`, which is why fleet work lives on a parked")
    print("branch with updates.parked_branch_strategy: update_in_place. See")
    print("BLOCKED-BOARD-DIAGNOSIS-2026-09-02.md in the hermes management folder.")
    return 0


def _load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(obj):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as fh:
            json.dump(obj, fh)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
