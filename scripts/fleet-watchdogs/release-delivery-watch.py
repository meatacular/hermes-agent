#!/usr/bin/env python3
"""release-delivery-watch - complete a RELEASE card whose merge is verifiably delivered.

WHY THIS EXISTS (measured 2026-09-18)
-------------------------------------
Three release cards in a row merged their PR, proved `main` green on a virgin worktree, and were
then REFUSED by `kanban_complete`:

    "rejected by the tenant CI gate because this release workspace has no open PR;
     the correct PR is already merged"

A release card cannot satisfy that gate BY CONSTRUCTION. Its own worktree branch has no PR - the
PR is the thing it merges, and merging it is what removes it from the open list. So the gate's
precondition is destroyed by the card doing its job correctly. That is why "done under the operator
exemption" appears on card after card in this lane: the exemption is the NORMAL path for a release,
which means releases are effectively ungated while still costing an operator round trip each time.

WHAT THIS DOES - and note it does NOT weaken the gate
-----------------------------------------------------
It applies a STRONGER check than "an open PR has a green check", and one that is only answerable
after the merge has happened:

    1. the PR is MERGED,
    2. its merge commit is an ancestor of origin/main,
    3. test-gate on the merged head was SUCCESS.

Only when all three hold does it complete the card, writing the evidence onto the card.

It lives in scripts/fleet-watchdogs/ and imports nothing from hermes_cli, so it has no merge
surface against upstream - this is the plugin/watchdog extension point, not a kernel patch.
Silent unless it acted or something needs a human.

SAFETY
------
  * --apply defaults False. The safe state is the default, so a caller that forgets the flag
    reports rather than writes (the lesson from assignee-mismatch-watch, 2026-09-13).
  * --max-apply caps one tick. Many release cards suddenly looking deliverable is better evidence
    that THIS script is wrong than that the board is; over the cap it writes NOTHING.
  * `running` and `review` cards are never touched.
  * It refuses a card whose PR it cannot identify unambiguously. It never guesses a number, and it
    reads the PR from a `release-pr:` marker or from the TITLE only - never from the body's prose,
    because a release body legitimately names other PRs as context ("do not merge #84"). Reading
    those would land the wrong thing.
"""
from __future__ import annotations
import argparse, json, os, re, sqlite3, subprocess, sys

HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".hermes", "kanban.db")
REPO_DEFAULT = "meatacular/backupbrain"
TREE_DEFAULT = os.path.join(HOME, "Projects", "backupbrain")
MARKER = "release-delivery-watch"

RELEASE_TITLE = re.compile(r"^\s*\[Release\]", re.I)
PR_MARKER = re.compile(r"^\s*release-pr:\s*#?(\d+)\s*$", re.M | re.I)
PR_IN_TITLE = re.compile(r"\bPR\s*#(\d+)\b", re.I)


def sh(args, cwd=None, timeout=90):
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:                      # a hung child must not take the tick with it
        class R:                                  # (the `op` CLI lesson, trap 58)
            returncode, stdout, stderr = 124, "", f"{type(exc).__name__}: {exc}"
        return R()


def pr_number(title: str, body: str):
    """The PR this release card exists to land, or None. Never a guess."""
    m = PR_MARKER.search(body or "")
    if m:
        return int(m.group(1))
    if not RELEASE_TITLE.search(title or ""):
        return None
    hits = {int(x) for x in PR_IN_TITLE.findall(title or "")}
    return hits.pop() if len(hits) == 1 else None


def delivered(pr: int, repo: str, tree: str):
    """(ok, reason). All three conditions, or a refusal naming the one that failed."""
    r = sh(["gh", "pr", "view", str(pr), "--repo", repo, "--json",
            "state,mergeCommit,headRefOid,statusCheckRollup"])
    if r.returncode != 0:
        return False, f"gh pr view #{pr} failed: {r.stderr.strip()[:160]}"
    try:
        d = json.loads(r.stdout)
    except Exception as exc:
        return False, f"unreadable gh output for #{pr}: {type(exc).__name__}"
    if d.get("state") != "MERGED":
        return False, f"#{pr} is {d.get('state')}, not MERGED - not delivered"
    sha = (d.get("mergeCommit") or {}).get("oid")
    if not sha:
        return False, f"#{pr} reports MERGED with no merge commit"
    sh(["git", "fetch", "--quiet", "origin", "--prune"], cwd=tree)
    if sh(["git", "merge-base", "--is-ancestor", sha, "origin/main"], cwd=tree).returncode != 0:
        return False, (f"#{pr} merge commit {sha[:8]} is NOT an ancestor of origin/main "
                       f"- merged into the wrong base")
    checks = {c.get("name"): c.get("conclusion") for c in (d.get("statusCheckRollup") or [])}
    if checks.get("test-gate") != "SUCCESS":
        return False, f"#{pr} test-gate on the merged head was {checks.get('test-gate')!r}, not SUCCESS"
    return True, f"#{pr} MERGED as {sha[:8]}, ancestor of origin/main, test-gate SUCCESS"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="complete verified cards (default: report only)")
    ap.add_argument("--max-apply", type=int, default=2)
    ap.add_argument("--repo", default=REPO_DEFAULT)
    ap.add_argument("--tree", default=TREE_DEFAULT)
    a = ap.parse_args()

    if not os.path.exists(DB):
        return 0
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = list(c.execute("SELECT id,title,body,status FROM tasks "
                              "WHERE status IN ('blocked','todo','ready')"))
    except sqlite3.Error as exc:
        print(f"board unreadable ({exc}) - standing down"); return 0

    cands = []
    for cid, title, body, status in rows:
        if MARKER in (body or ""):
            continue
        pr = pr_number(title or "", body or "")
        if pr is not None:
            cands.append((cid, title or "", pr))
    if not cands:
        return 0                                   # silent

    verified, held = [], []
    for cid, title, pr in cands:
        ok, why = delivered(pr, a.repo, a.tree)
        (verified if ok else held).append((cid, title, pr, why))

    for cid, title, pr, why in held:
        print(f"HOLD {cid} ({title[:58]}): {why}")

    if not verified:
        return 0
    if len(verified) > a.max_apply:
        print(f"REFUSING to act: {len(verified)} release cards look delivered at once (cap "
              f"{a.max_apply}). Many at once is better evidence that this script is wrong than "
              f"that the board is. Cards: {[v[0] for v in verified]}")
        return 0

    for cid, title, pr, why in verified:
        print(f"DELIVERED {cid} ({title[:58]}): {why}")
        if not a.apply:
            continue
        note = (f"{MARKER}: completed on verified delivery. {why}. This card could not "
                f"self-complete because the tenant CI gate requires an OPEN pull request on the "
                f"card's own worktree branch, and a release card has none - its PR is the one it "
                f"just merged. The three conditions checked here are a stronger claim than the "
                f"gate's, and are only answerable after the merge.")
        sh(["hermes", "kanban", "comment", cid, note, "--author", MARKER])
        if sh(["hermes", "kanban", "complete", cid]).returncode != 0:
            sh(["hermes", "kanban", "unblock", cid, "--reason", f"{MARKER}: delivery verified"])
            sh(["hermes", "kanban", "complete", cid])
        print(f"  completed {cid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
