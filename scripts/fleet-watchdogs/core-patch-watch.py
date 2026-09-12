#!/usr/bin/env python3
"""core-patch-watch — has anything patched upstream's kernel behind our back?

The 2026-09-09 decision is that policy lives in plugins, hooks, watchdogs and SOUL
rules, and upstream's kanban stays the SUBSTRATE. On 2026-09-12 that decision was
written down, carried in three SOULs, and enforced by nothing: a batch of
board-improvement cards patched the kernel anyway, one merge rewrote
tools/kanban_tools.py by +3891/-1945 and silently dropped code another card had added
ninety minutes earlier, and three more cards were minted to repair the damage.

`kanban-mint-guard` catches the unambiguous case at MINT time, from the card body.
This catches everything it cannot, because it reads COMMITS — ground truth, no prose
to classify, and no way for a path (the auto-decomposer, a worker committing
directly, a merge) to route around it.

Exempt, because these are how the kernel is SUPPOSED to change:
  * anything already reachable from an attended catch-up tag (upd-*, catchup*) —
    that is upstream's own code arriving through the attended process;
  * a commit whose message carries `core-patch-approved:` (Richie said yes);
  * a revert, which is the kernel moving back toward upstream, never away.

Silent when clean. Read-only: it never writes to the repo or the board.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
REPO = Path(os.environ.get("CORE_PATCH_WATCH_REPO") or HERMES_HOME / "hermes-agent")
STATE = Path(os.environ.get("CORE_PATCH_WATCH_STATE")
             or HERMES_HOME / "state" / "core-patch-watch.json")

KERNEL_DIRS = ("hermes_cli/", "tools/", "agent/", "gateway/")
APPROVED = "core-patch-approved:"
# ONLY the attended upstream catch-ups. NOT prerollback/* or retired/* — those tags
# point AT the commits this is meant to find (they mark the state before a rollback),
# so exempting them would hide exactly the thing being watched for.
EXEMPT_TAG_GLOBS = ("upd-*", "catchup*")


def git(*args: str, cwd: Path | None = None) -> str:
    """Read-only git. --no-optional-locks so this never leaves an index.lock behind —
    a stale one blocks the fleet's own merges (learned the hard way 2026-09-12)."""
    out = subprocess.run(["git", "--no-optional-locks", *args], cwd=str(cwd or REPO),
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip() if out.returncode == 0 else ""


def kernel_files(sha: str) -> list[str]:
    names = git("show", "--name-only", "--format=", sha).splitlines()
    return sorted({
        n for n in (x.strip() for x in names)
        if n and n.startswith(KERNEL_DIRS) and not n.startswith("tests/")
    })


def exempt_shas() -> set[str]:
    """Every commit reachable from an attended catch-up / archival tag."""
    shas: set[str] = set()
    for glob in EXEMPT_TAG_GLOBS:
        for tag in git("tag", "--list", glob).splitlines():
            tag = tag.strip()
            if not tag:
                continue
            shas.update(x for x in git("rev-list", tag).splitlines() if x)
    return shas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="explicit start ref (default: the last run's HEAD)")
    ap.add_argument("--branch", default="fleet")
    ap.add_argument("--no-state", action="store_true", help="do not record HEAD")
    a = ap.parse_args()
    if not (REPO / ".git").exists():
        return 0

    head = git("rev-parse", a.branch)
    if not head:
        print(f"core-patch-watch: cannot resolve {a.branch} in {REPO}")
        return 0

    try:
        prev = json.loads(STATE.read_text()).get("head")
    except Exception:  # noqa: BLE001
        prev = None
    since = a.since or prev
    if not since:
        # First run: record where we are and say nothing. Reporting the whole of
        # history on day one would be noise nobody reads, and would train the reader
        # to ignore this watchdog — which is the failure mode it exists to avoid.
        if not a.no_state:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"head": head, "note": "baseline, nothing reported"}))
        return 0

    rng = f"{since}..{head}"
    shas = [x for x in git("rev-list", rng).splitlines() if x]
    exempt = exempt_shas() if shas else set()

    findings = []
    for sha in shas:
        if sha in exempt:
            continue
        subject = git("log", "-1", "--format=%s", sha)
        full = git("log", "-1", "--format=%B", sha)
        if APPROVED in full.lower():
            continue
        if subject.lower().startswith(("revert", "revert(")):
            continue
        files = kernel_files(sha)
        if files:
            findings.append((sha[:11], git("log", "-1", "--format=%an", sha), subject, files))

    if not a.no_state:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"head": head, "last_range": rng}))

    if not findings:
        return 0                                   # silent: the normal case

    print(f"🔧 core-patch-watch: {len(findings)} commit(s) changed upstream's KERNEL on "
          f"`{a.branch}` ({rng})")
    print("   Policy (2026-09-09): policy lives in plugins/hooks/watchdogs/SOULs; "
          "hermes_cli/, tools/, agent/ and gateway/ are upstream's.")
    for sha, who, subject, files in findings:
        print(f"  {sha}  {who}  {subject[:80]}")
        for f in files[:8]:
            print(f"      {f}")
        if len(files) > 8:
            print(f"      … and {len(files) - 8} more")
    print("   If one of these is approved, add a `core-patch-approved: <who>` line to its "
          "commit message, or tag the range. Otherwise it should be reverted and "
          "re-expressed — see hermes management/BACKLOG.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
