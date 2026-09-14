#!/usr/bin/env python3
"""upstream-update-watch — charter §11 / upgrade plan §2–§3, 2026-09-03.

Daily, zero tokens. Fetches upstream and tiers the pending update by FILE OVERLAP:

    BASE  = merge-base fleet origin/main
    A     = files changed fleet...origin/main         (what upstream changed)
    B     = files changed BASE..fleet                 (what the fleet patched)
    gate  = A ∩ B

  Tier A (gate empty)  → provably safe from the fleet's point of view. With
                         --apply: merge the PINNED SHA as an ordinary merge
                         commit, run focused tests, restart root, log pre/post.
                         Without --apply (the default for the first week):
                         report "would apply".
  Tier B (gate non-empty) → NEVER merged here. Posts the overlap list, the
                         commit range and a compare link for the pinned SHA;
                         waits for Richie.

Guards: never on a dirty tree, never off `fleet`, never force; a conflict
aborts the merge and reports. Every applied merge writes a change manifest
(change-manifest.py) so fleet-rollback.sh can revert it.

Dedup: state/upstream-update-watch.json remembers the last reported SHA so a
standing Tier B is posted once per new upstream tip, not daily forever.
Test overrides: UPSTREAM_WATCH_REPO (scratch repo), UPSTREAM_WATCH_STATE,
UPSTREAM_WATCH_REMOTE_BRANCH (default origin/main).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
REPO = Path(os.environ.get("UPSTREAM_WATCH_REPO") or HERMES_HOME / "hermes-agent")
STATE = Path(os.environ.get("UPSTREAM_WATCH_STATE") or HERMES_HOME / "state" / "upstream-update-watch.json")
UP = os.environ.get("UPSTREAM_WATCH_REMOTE_BRANCH") or "origin/main"
FLEET = "fleet"
COMPARE = "https://github.com/NousResearch/hermes-agent/compare/{a}...{b}"
# Hard floor for the gate (upgrade plan §2): files the fleet has patched are ALWAYS
# Tier B when upstream touches them, whatever the merge-base arithmetic says. The
# 09-03 test showed the computed set can be empty when the incoming tip descends
# from fleet itself. Recompute B from history too, then union with this list.
FLEET_CORE_FILES = {
    "agent/aux_accounting.py", "agent/codex_runtime.py", "agent/conversation_loop.py",
    "agent/kanban_checkpoint.py", "agent/kanban_stop.py", "cli-config.yaml.example", "cli.py",
    "gateway/kanban_watchers.py", "gateway/run.py", "gateway/status.py", "gateway/stream_consumer.py",
    "hermes_cli/config_defaults.py", "hermes_cli/kanban.py", "hermes_cli/kanban_db.py",
    "hermes_cli/kanban_decompose.py", "hermes_cli/kanban_swarm.py", "hermes_state.py",
    "hermes_state_common.py", "hermes_state_schema.py", "plugins/kanban-block-escalator/__init__.py",
    "plugins/kanban-block-escalator/plugin.yaml", "pyproject.toml", "tools/kanban_tools.py",
    "hermes_cli/update_cmd.py",  # fleet update gate (09-03)
}
FLEET_CORE_PREFIXES = ("scripts/fleet-watchdogs/",)
# Schema/migration guard (09-03): a git revert cannot un-migrate a database, so an
# update that touches schema is never Tier A — it is reported as Tier B-schema and
# waits for Richie, who takes it attended with a db snapshot in the manifest.
import re as _re
SCHEMA_FILE_HINTS = ("schema", "migration", "migrate")
SCHEMA_DIFF_RE = _re.compile(r"^\+.*\b(ALTER TABLE|CREATE TABLE|DROP TABLE|CREATE INDEX|DROP INDEX|PRAGMA user_version|_add_column\(|add_column\()",
                             _re.I | _re.M)


def schema_touches(repo_git, base, tip):
    """Files in base..tip whose name or added lines look like a schema change."""
    rc, names, _ = repo_git("diff", "--name-only", f"{base}..{tip}")
    hits = set()
    for f in names.splitlines():
        if any(h in f.lower() for h in SCHEMA_FILE_HINTS) and f.endswith((".py", ".sql")):
            hits.add(f)
    rc, diff, _ = repo_git("diff", "--unified=0", f"{base}..{tip}", "--", "*.py", "*.sql")
    cur = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif cur and SCHEMA_DIFF_RE.match(line):
            hits.add(cur)
    return sorted(hits)
# ── Arming gate for the unattended apply path (2026-09-07) ───────────
# Tier decides whether a merge is *provably non-overlapping*. It does NOT decide
# whether an unattended merge is a good idea, and until today nothing did: the
# `--apply` branch was chosen by `tier == "A"` alone.
#
# Three failures that gate is built from, all measured:
#   * SIZE. `fleet` is 4,764 commits behind. Tier is computed from the file
#     overlap, so a quiet fortnight upstream could flip a gap of that size to
#     Tier A and hand an unattended process the largest merge this fleet has
#     ever done. A merge nobody watched is not made safe by being conflict-free.
#   * SETTLEMENT. Measured at 71.9% refactor churn with `simp/*` merges landing
#     daily. The predicate that says so was already computed and already
#     ignored on this path.
#   * ONE READING IS NOT A TREND. Settlement is a daily snapshot of a 14-day
#     window; arming on a single ok:true makes one noisy day the whole control.
UPSTREAM_MAX_UNATTENDED = int(os.environ.get("UPSTREAM_MAX_UNATTENDED_COMMITS", "50"))
SETTLE_CONSECUTIVE_DAYS = int(os.environ.get("UPSTREAM_SETTLE_CONSECUTIVE_DAYS", "3"))


def _settled_streak(jsonl):
    """How many consecutive recent DAYS reported settled at canonical thresholds.

    Reads this watch's own log. Only rows carrying `settle.canonical` are
    counted, so a run with the thresholds loosened by env var — which is how the
    predicate gets proved — can never arm the merge it was testing.
    """
    try:
        rows = [json.loads(x) for x in jsonl.read_text().splitlines() if x.strip()]
    except Exception:  # noqa: BLE001
        return 0
    by_day = {}
    for r in rows:
        s = r.get("settle") or {}
        if not s.get("canonical"):
            continue  # a threshold-varying test run, not a verdict
        day = time.strftime("%Y-%m-%d", time.localtime(r.get("at", 0)))
        # Worst reading of the day wins: one unsettled read makes the day unsettled.
        by_day[day] = by_day.get(day, True) and bool(s.get("ok"))
    streak = 0
    for day in sorted(by_day, reverse=True):
        if by_day[day]:
            streak += 1
        else:
            break
    return streak


def arming(settle, behind, schema):
    """May an unattended `--apply` proceed? Every condition must hold."""
    streak = _settled_streak(HERMES_HOME / "logs" / "upstream-update-watch.jsonl")
    checks = [
        (not schema, f"{len(schema)} schema/migration file(s) — never automated, "
                     f"a revert cannot un-migrate a database"),
        (behind <= UPSTREAM_MAX_UNATTENDED,
         f"{behind} commits behind, over the unattended ceiling of {UPSTREAM_MAX_UNATTENDED} "
         f"— take a gap this size attended, once, then the daily delta is small enough"),
        (bool(settle.get("ok")), f"upstream not settled ({settle.get('reason')})"),
        (streak >= SETTLE_CONSECUTIVE_DAYS,
         f"settled on only {streak} consecutive canonical reading(s), need "
         f"{SETTLE_CONSECUTIVE_DAYS} — one day is a snapshot, not a trend"),
    ]
    failed = [why for ok, why in checks if not ok]
    return {"ok": not failed, "streak": streak, "behind": behind,
            "max_unattended": UPSTREAM_MAX_UNATTENDED,
            "reason": "armed" if not failed else "; ".join(failed)}


FOCUSED_TESTS = [
    "tests/hermes_cli/test_kanban_deploy_followup.py",
    "tests/hermes_cli/test_kanban_block_escalator.py",
    "tests/hermes_cli/test_kanban_decompose.py",
    "tests/tools/test_kanban_tools.py",
]


# ── Settlement trigger (2026-09-07) ──────────────────────────────────
# Upstream was measured at 71% refactor+simplify across 4,760 commits with
# `simp/*` integration merges landing daily: a codebase mid-restructure. Taking
# it then means paying the conflict cost of unfinished work twice. These two
# numbers decide when that has passed, so the answer comes from a measurement
# rather than from someone remembering to look.
SETTLE_WINDOW_DAYS = int(os.environ.get("UPSTREAM_SETTLE_WINDOW_DAYS", "14"))
SETTLE_MAX_REFACTOR_PCT = float(os.environ.get("UPSTREAM_SETTLE_MAX_REFACTOR_PCT", "30"))
SETTLE_MIN_QUIET_DAYS = int(os.environ.get("UPSTREAM_SETTLE_MIN_QUIET_DAYS", "7"))


def settlement(repo_git, base, tip):
    """Is upstream settled enough to be worth merging?

    Returns a dict; never raises — an unreadable log must not block the report.
    """
    out = {"ok": False, "refactor_pct": None, "quiet_days": None,
           "sample": 0, "reason": "unmeasured"}
    try:
        since = f"--since={SETTLE_WINDOW_DAYS}.days.ago"
        _, subjects, _ = repo_git("log", "--format=%s", since, f"{base}..{tip}")
        subs = [x for x in subjects.splitlines() if x.strip()]
        out["sample"] = len(subs)
        if not subs:
            out.update(reason="no upstream commits in the window", ok=True,
                       refactor_pct=0.0)
        else:
            churn = sum(1 for x in subs
                        if x.startswith(("refactor", "simplify", "simp(")))
            out["refactor_pct"] = round(100.0 * churn / len(subs), 1)
        # Recency of the last integration merge, in days.
        _, last, _ = repo_git("log", "-1", "--format=%ct", "--grep=simp/",
                              f"{base}..{tip}")
        last = (last or "").strip()
        if last:
            out["quiet_days"] = round((time.time() - float(last)) / 86400.0, 1)
        else:
            out["quiet_days"] = 999.0  # none in range: as quiet as it gets
        if out["refactor_pct"] is not None:
            calm = out["refactor_pct"] < SETTLE_MAX_REFACTOR_PCT
            quiet = out["quiet_days"] >= SETTLE_MIN_QUIET_DAYS
            out["ok"] = bool(calm and quiet)
            # Record the thresholds in EVERY row, passing or failing (2026-09-07).
            # A failing row carried them inside `reason`; a passing row just said
            # "settled". So on 09-07, when the predicate was being proved by
            # varying the thresholds by env var, three records landed within five
            # seconds and the ok:true one was indistinguishable in the log from a
            # genuine settlement. A verdict you cannot audit after the fact is not
            # evidence, and the arming gate below reads these rows.
            out["thresholds"] = {"window_days": SETTLE_WINDOW_DAYS,
                                 "max_refactor_pct": SETTLE_MAX_REFACTOR_PCT,
                                 "min_quiet_days": SETTLE_MIN_QUIET_DAYS}
            out["canonical"] = bool(SETTLE_WINDOW_DAYS == 14
                                    and SETTLE_MAX_REFACTOR_PCT == 30
                                    and SETTLE_MIN_QUIET_DAYS == 7)
            out["reason"] = (
                "settled" if out["ok"] else
                f"{'churn ' + str(out['refactor_pct']) + '% >= ' + str(SETTLE_MAX_REFACTOR_PCT) + '%' if not calm else ''}"
                f"{' and ' if (not calm and not quiet) else ''}"
                f"{'last simp/ merge ' + str(out['quiet_days']) + 'd ago < ' + str(SETTLE_MIN_QUIET_DAYS) + 'd' if not quiet else ''}"
            )
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"unmeasured ({exc})"
    return out


def git(*args, timeout=120):
    p = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    os.environ["HERMES_FLEET_GATED_UPDATE"] = "1"  # the only sanctioned update path on `fleet`
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Tier A: actually merge (default: report only)")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    for lock in ("index.lock", "ORIG_HEAD.lock"):
        try:
            (REPO / ".git" / lock).unlink()
        except FileNotFoundError:
            pass
    try:
        st = json.loads(STATE.read_text()) if STATE.exists() else {}
    except Exception:  # noqa: BLE001
        st = {}

    rc, br, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or br != FLEET:
        print(f"🛑 upstream-update-watch: checkout is on {br!r}, not {FLEET} — stopping (charter §11)")
        return 0
    rc, dirty, _ = git("status", "--porcelain")
    if dirty:
        print(f"🛑 upstream-update-watch: tree dirty ({len(dirty.splitlines())} files) — no merge onto a dirty tree")
        return 0
    if not a.no_fetch:
        rc, _, err = git("fetch", UP.split("/")[0], "--quiet")
        if rc != 0:
            print(f"⚠️ upstream-update-watch: fetch failed: {err[:200]}")
            return 0
    _, head, _ = git("rev-parse", "HEAD")
    _, tip, _ = git("rev-parse", UP)
    _, base, _ = git("merge-base", FLEET, UP)
    _, behind, _ = git("rev-list", "--count", f"{FLEET}..{UP}")
    _, ahead, _ = git("rev-list", "--count", f"{UP}..{FLEET}")
    if int(behind or 0) == 0:
        if a.verbose:
            print("up to date")
        return 0
    _, up_changed, _ = git("diff", "--name-only", f"{FLEET}...{UP}")
    _, fleet_patched, _ = git("diff", "--name-only", f"{base}..{FLEET}")
    A = set(up_changed.splitlines()) - {""}
    B = (set(fleet_patched.splitlines()) - {""}) | FLEET_CORE_FILES
    gate = sorted(f for f in A if f in B or f.startswith(FLEET_CORE_PREFIXES))
    schema = schema_touches(git, base, tip)
    settle = settlement(git, base, tip)
    tier = "B" if (gate or schema) else "A"
    arm = arming(settle, int(behind), schema)
    key = f"{tier}:{tip}"
    link = COMPARE.format(a=base[:12], b=tip[:12])
    rec = {"at": int(time.time()), "tier": tier, "head": head[:12], "tip": tip[:12], "base": base[:12],
           "behind": int(behind), "ahead": int(ahead), "gate": gate, "schema": schema, "settle": settle,
           "arm": arm, "applied": False}

    if tier == "B" or not a.apply or not arm["ok"]:
        if st.get("last_reported") == key and not a.verbose:
            return 0  # already told Richie about this exact tip
        head_line = (f"*Upstream update pending — Tier {tier}* ({behind} commits behind `{UP}`, {ahead} ahead; "
                     f"pinned tip `{tip[:12]}`, base `{base[:12]}`)")
        lines = [head_line]
        if tier == "B":
            if gate:
                lines.append(f"  {len(gate)} fleet-patched file(s) also changed upstream — NOT merged, needs your review of just these:")
                lines += [f"    - {f}" for f in gate[:30]]
            if schema:
                lines.append(f"  SCHEMA/MIGRATION change detected in {len(schema)} file(s) — never auto-applied (a revert cannot un-migrate a database); "
                             f"take it attended with `change-manifest.py db <id>` first:")
                lines += [f"    - {f}" for f in schema[:15]]
            if len(gate) > 30:
                lines.append(f"    … and {len(gate) - 30} more")
            lines.append(f"  Review the pinned range: {link}")
            lines.append(f"  Then: `git merge --no-ff {tip[:12]}` on `fleet` (attended, scratch branch first per upgrade plan §4).")
        else:
            lines.append(f"  No overlap with the {len(B)} fleet-patched files → provably safe on the overlap test alone.")
            lines.append(f"  Range: {link}")
        # Settlement and arming are reported for BOTH tiers (2026-09-07). They used
        # to print only inside the Tier B branch, so an unsettled upstream was not
        # merely un-gated on the Tier A apply path — it was not even mentioned in
        # the run that would have performed the merge.
        _s = settle
        if _s.get("refactor_pct") is not None:
            lines.append(
                f"  Settlement: {_s['refactor_pct']}% refactor/simplify over the last "
                f"{SETTLE_WINDOW_DAYS}d ({_s['sample']} commits), last simp/ merge "
                f"{_s['quiet_days']}d ago -> "
                + ("SETTLED, worth planning the merge now." if _s["ok"]
                   else f"NOT settled ({_s['reason']}) — merging into an unfinished "
                        "restructure pays the conflict cost twice.")
            )
        if a.apply and not arm["ok"]:
            lines.append(f"  UNATTENDED APPLY WITHHELD — {arm['reason']}. Reported, not merged.")
        elif tier == "A" and not a.apply:
            lines.append("  Tier A, but `--apply` is not on this invocation, so this is a report. "
                         f"Arming check says: {arm['reason']}.")
        print("\n".join(lines))
        st["last_reported"] = key
        rec["reported"] = True
    else:
        # Tier A, --apply: merge the pinned SHA, test, restart, manifest.
        cm = HERMES_HOME / "scripts" / "change-manifest.py"
        cid = f"upstream-{time.strftime('%Y%m%d-%H%M%S')}-{tip[:8]}"  # unique per apply (a minute-resolution id collided in the 09-03 test)
        subprocess.run([sys.executable, str(cm), "new", cid, "--title", f"upstream merge {tip[:12]} (Tier A, {behind} commits)", "--restart", "root,axel,switch,brain"], check=False)  # break-test 09-03: a root-only restart does not decide which code dispatches
        subprocess.run([sys.executable, str(cm), "db", cid], check=False)  # data snapshot: rollback restores rows too
        rc, out, err = git("merge", "--no-ff", "--no-edit", tip, timeout=300)
        if rc != 0:
            git("merge", "--abort")
            print(f"🛑 upstream-update-watch: Tier A merge of {tip[:12]} CONFLICTED and was aborted — treat as Tier B:\n{(out + err)[-600:]}")
            rec["conflict"] = True
        else:
            _, post, _ = git("rev-parse", "HEAD")
            subprocess.run([sys.executable, str(cm), "git", cid, str(REPO), "--pre", head, "--post", post, "--branch", FLEET], check=False)
            py = REPO / "venv" / "bin" / "python"
            try:
                t = subprocess.run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", *FOCUSED_TESTS],
                                   cwd=REPO, capture_output=True, text=True, timeout=900)
                tests_ok, t_out = t.returncode == 0, t.stdout
            except Exception as e:  # noqa: BLE001 — no interpreter = not proven = roll back
                tests_ok, t_out = False, f"could not run focused tests: {e}"
            rec.update({"applied": True, "post": post[:12], "tests_ok": tests_ok})
            if tests_ok:
                subprocess.run([sys.executable, str(cm), "applied", cid], check=False)
                # Restart ALL gateways that can hold the dispatcher lease: the break-test on
                # 09-03 showed axel's process dispatching from its stale in-memory kanban_db
                # after a root-only restart. Order: root, axel (from here — this runs on
                # axel's cron, so axel's restart is last and self-inflicted-safe via launchd), switch.
                outs = []
                # brain added 2026-09-12: it holds a gateway and CAN take the dispatcher lease,
                # and pre-flight check 11 compares every profile in the manifest's restart list
                # against applied_at — so omitting it makes an unattended apply go RED for no
                # fault. The desktop backend and the dashboard are long-lived Hermes processes
                # too (trap 24) and pre-flight check 13 covers them; refresh them last.
                for who, sc in (("root", "profiles/axel/scripts/restart-root-gateway.sh"),
                                ("switch", "scripts/switch-gw-restart.sh"),
                                ("brain", "scripts/restart-brain-gateway.sh"),
                                ("axel", "scripts/restart-axel-gateway.sh"),
                                ("desktop+dashboard", "scripts/restart-desktop-and-dashboard.sh")):
                    rr = subprocess.run(["/bin/bash", str(HERMES_HOME / sc)], capture_output=True, text=True, timeout=300)
                    outs.append(f"{who}: {((rr.stdout or rr.stderr).strip().splitlines() or ['?'])[-1][:60]}")
                class _R:  # keep the summary line below unchanged
                    stdout = "; ".join(outs); stderr = ""
                r = _R()
                print(f"✅ upstream-update-watch: applied {behind} upstream commits ({head[:12]}→{post[:12]}), no fleet-file overlap, "
                      f"focused tests green, root gateway: {(r.stdout or r.stderr).strip().splitlines()[-1:] or ['?']}. "
                      f"Rollback: `fleet-rollback.sh {cid}`.")
                # Canary (09-03): prove the new code dispatches; on FAIL it rolls THIS manifest back.
                cn = subprocess.run([sys.executable, str(HERMES_HOME / "scripts" / "post-apply-canary.py"),
                                     "--manifest", cid, "--rollback-on-fail"], capture_output=True, text=True, timeout=1200)
                print((cn.stdout or cn.stderr).strip()[-600:])
                rec["canary_rc"] = cn.returncode
            else:
                print(f"🛑 upstream-update-watch: merged {tip[:12]} but focused tests FAILED — rolling back via manifest {cid}\n{t_out[-800:]}")
                subprocess.run([sys.executable, str(cm), "applied", cid], check=False)
                subprocess.run([sys.executable, str(cm), "rollback", cid, "--no-restart"], check=False)
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, indent=1))
        with (HERMES_HOME / "logs" / "upstream-update-watch.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
