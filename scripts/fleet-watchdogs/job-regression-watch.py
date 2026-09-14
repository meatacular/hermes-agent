#!/usr/bin/env python3
"""job-regression-watch — charter §7 regression suite at the end of every job
(build-list item 16).

Convention: every product has `scripts/regress.sh` running `tests/regression/`,
finishing inside ten minutes, exiting non-zero on any failure.

Trigger: a parent card that has a `Deploy:` child sitting in
`blocked/operator_hold`, all of whose OTHER children are terminal (done or
archived), and whose deploy card carries no `regression:` comment yet. That is
"after the last QA card and before the deploy card is released".

Action (zero tokens unless something fails):
  * no `scripts/regress.sh` in the repo -> comment `regression: NO SUITE` on the
    deploy card (once) and tell Richie — charter §7 says the first job on a
    product seeds one.
  * suite green -> comment `regression: GREEN <sha> <runtime>s` on the deploy
    card and the parent. Silent.
  * suite red -> comment `regression: FAILED`, mint a remediation card to Bob
    under the same parent (capped by policy), and tell Richie. The deploy card
    stays held — it is already `operator_hold`, and the failure comment is what
    the release checklist reads.

Runs natively (root cron; the suite executes in the project repo). Writes
`logs/job-regression.jsonl` for the ledger. Test overrides:
REGRESS_DB, REGRESS_STATE, REGRESS_DRYRUN=1 (no comments/cards, prints intent),
FLEET_NOTIFY_DRYRUN.
"""
import json
import os
import sqlite3
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
DB = Path(os.environ.get("REGRESS_DB") or HERMES_HOME / "kanban.db")
STATE = Path(os.environ.get("REGRESS_STATE") or HERMES_HOME / "state" / "job-regression-watch.json")
LOG = HERMES_HOME / "logs" / "job-regression.jsonl"
HERMES_BIN = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes"
DRY = bool(os.environ.get("REGRESS_DRYRUN"))
TIMEOUT_S = 600
TERMINAL = ("done", "archived")


def hermes(*args):
    if DRY:
        print("  (dry) hermes " + " ".join(str(a) for a in args))
        return 0, ""
    try:
        p = subprocess.run([str(HERMES_BIN), *args], capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001 — a missing CLI must not crash the watch
        print(f"  hermes CLI failed: {e}")
        return 127, str(e)


def comment(tid, text):
    return hermes("kanban", "comment", tid, text, "--author", "job-regression-watch")


def candidates(c):
    rows = c.execute(
        "SELECT d.id AS deploy_id, d.title AS deploy_title, l.parent_id, p.title AS parent_title, "
        "       p.workspace_path AS p_ws, d.workspace_path AS d_ws, p.tenant "
        "FROM tasks d JOIN task_links l ON l.child_id=d.id JOIN tasks p ON p.id=l.parent_id "
        "WHERE d.status='blocked' AND d.block_kind='operator_hold' AND (d.title LIKE 'Deploy:%' OR lower(d.title) LIKE '%deploy%' OR lower(d.title) LIKE '%release%' OR lower(d.title) LIKE '%rollout%' OR lower(d.title) LIKE '%go live%') "
        "AND NOT EXISTS (SELECT 1 FROM task_comments tc WHERE tc.task_id=d.id AND tc.body LIKE 'regression:%')"
    ).fetchall()
    out = []
    for r in rows:
        others = c.execute(
            "SELECT t.id, t.status, t.workspace_path FROM tasks t JOIN task_links l ON l.child_id=t.id "
            "WHERE l.parent_id=? AND t.id!=?", (r["parent_id"], r["deploy_id"]),
        ).fetchall()
        if any(o["status"] not in TERMINAL for o in others):
            continue
        ws = r["p_ws"] or r["d_ws"] or next((o["workspace_path"] for o in others if o["workspace_path"]), None)
        out.append((r, ws))
    return out


def repo_root(ws):
    if not ws:
        return None
    p = Path(ws)
    # worktrees live at <repo>/.worktrees/<id>; strip back to the repo
    for anc in [p] + list(p.parents):
        if anc.name == ".worktrees":
            return anc.parent
    for anc in [p] + list(p.parents):
        if (anc / ".git").exists():
            return anc
    return p if p.exists() else None


def run_suite(repo):
    script = repo / "scripts" / "regress.sh"
    if not script.exists():
        return "no-suite", None, "", None
    sha = ""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=10", "HEAD"], cwd=repo, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    if DRY:
        return "dry", 0, "(dry run — suite not executed)", sha
    t0 = time.time()
    try:
        p = subprocess.run(["/bin/bash", str(script)], cwd=repo, capture_output=True, text=True, timeout=TIMEOUT_S)
        out = (p.stdout + p.stderr)[-3000:]
        return ("green" if p.returncode == 0 else "red"), round(time.time() - t0, 1), out, sha
    except subprocess.TimeoutExpired:
        return "timeout", TIMEOUT_S, f"regress.sh exceeded {TIMEOUT_S}s (charter §7: must finish inside ten minutes)", sha


def main() -> int:
    if not DB.exists():
        return 0
    try:
        state = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        state = {}
    seen = state.setdefault("seen", {})
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    lines = []
    for r, ws in candidates(c):
        did = r["deploy_id"]
        if did in seen and not DRY:
            continue
        repo = repo_root(ws)
        rec = {"at": int(time.time()), "deploy": did, "parent": r["parent_id"], "repo": str(repo) if repo else None}
        if repo is None:
            verdict, runtime, out, sha = "no-repo", None, f"workspace {ws!r} does not resolve to a repo", None
        else:
            verdict, runtime, out, sha = run_suite(repo)
        rec.update({"verdict": verdict, "runtime_s": runtime, "sha": sha})
        if verdict == "green":
            comment(did, f"regression: GREEN {sha} in {runtime}s — deploy may be released")
            comment(r["parent_id"], f"regression: GREEN {sha} in {runtime}s (deploy {did})")
        elif verdict in ("no-suite", "no-repo"):
            comment(did, f"regression: NO SUITE — {out or 'scripts/regress.sh missing'} (charter §7: seed one before releasing)")
            lines.append(f"*Regression suite missing* for job `{r['parent_id']}` ({(r['parent_title'] or '')[:60]}): "
                         f"{out or 'scripts/regress.sh missing'} in {repo}. Deploy `{did}` stays held; a Steve-o seed-suite card is needed.")
        elif verdict == "dry":
            lines.append(f"(dry) would run {repo}/scripts/regress.sh for deploy {did}")
        else:
            comment(did, f"regression: FAILED ({verdict}, {runtime}s, {sha}) — deploy HELD. Tail:\n{out[-1200:]}")
            title = f"Regression failure after job {r['parent_id']}: fix and extend tests/regression"
            rc, res = hermes("kanban", "create", title,
                             "--assignee", "bob", "--parent", r["parent_id"],
                             "--workspace", f"dir:{repo}",
                             "--body", f"scripts/regress.sh went {verdict} at {sha} after the last QA card of "
                                       f"{r['parent_id']}. Make it green without weakening it; add a case for the "
                                       f"defect. Tail of the run:\n\n{out[-1500:]}",
                             "--idempotency-key", f"regress-fix:{did}:{sha}")
            rec["remediation"] = res[-200:]
            lines.append(f"*Regression FAILED* after job `{r['parent_id']}` — deploy `{did}` held, remediation card minted for Bob "
                         f"({verdict}, {runtime}s at {sha}). Last lines:\n```{out[-600:]}```")
        seen[did] = rec["at"]
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass
    if not DRY:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1))
            tmp.replace(STATE)
        except Exception:  # noqa: BLE001
            pass
    if lines:
        text = "\n".join(lines)
        print(text)
        slack_dm(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
