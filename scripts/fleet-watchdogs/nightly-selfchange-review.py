#!/usr/bin/env python3
"""nightly-selfchange-review — what did the fleet change about ITSELF today? (2026-09-16)

Richie's cadence, stated 2026-09-16: "every night reviewing changes made by the hermes system to
itself, assessing the quality of their work in the context of their purpose, if required adapting
the work so it complies with requirements of the system, and adapting the system so it makes higher
quality changes and decisions next time."

This is the EVIDENCE half and it costs nothing: stdlib only, read-only, no LLM. It writes
``logs/nightly-selfchange-<date>.md`` and prints a short signal line. The JUDGEMENT half is a Claude
session that reads this file — so the expensive half starts from facts instead of spending its first
twenty minutes discovering them, and the record exists even on a night the session never runs.

Deliberately NOT the same thing as ``self-improvement-review.py``. That one measures the BOARD
(reliability, cost, cycle time) and asks Smith to propose improvements. This one looks at the
fleet's changes to its own substrate — commits, manifests, plugins, watchdogs, skills, SOULs,
configs — and at whether those changes were made the way the system requires them to be made.

What it reports, and why each item is here rather than an arbitrary metric:

  * every commit on `fleet` in the window, with the files grouped by EXTENSION POINT. The
    extension-point rule is the fleet's central law; a change's category is the first thing a
    reviewer needs and the last thing a commit message reliably says.
  * **kernel touches** — the one thing that is supposed to be impossible. core-patch-watch already
    alerts on these; repeating it here means the nightly review cannot miss it.
  * **tests alongside code** — a plugin or watchdog commit with no test file in it is not
    necessarily wrong, but it is the shape that produced every phantom fix on this fleet.
  * **manifests** created vs applied, and any that are unapplied — an unapplied manifest is a
    change nobody can roll back cleanly.
  * **cards the fleet completed**, with per-worker cost, because the cap is per worker and a pooled
    figure has read as a breach three times.
  * **overwatch interventions** and their stored author, which was wrong fleet-wide until
    2026-09-15 and is the kind of regression only a daily read catches.
  * **the controls' own health**: pre-flight verdict, a parked apply-queue, watchdog failure
    streaks. A review that trusts the watchdogs without checking them is the fleet's oldest bug.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
REPO = HERMES_HOME / "hermes-agent"
DB = HERMES_HOME / "kanban.db"
CHANGES = HERMES_HOME / "changes"
WINDOW_H = float(os.environ.get("NIGHTLY_REVIEW_WINDOW_H", "24"))

KERNEL = ("hermes_cli/", "tools/", "agent/", "gateway/")


def git(*args):
    try:
        r = subprocess.run(["git", "--no-optional-locks", *args], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def classify(path: str) -> str:
    """Which extension point does this file belong to? The order matters: kernel first, because a
    kernel path inside a plugin-looking change is exactly what must not be missed."""
    if any(path.startswith(k) for k in KERNEL):
        return "KERNEL"
    if path.startswith("plugins/"):
        return "plugin"
    if "fleet-watchdogs/" in path or path.startswith("scripts/"):
        return "watchdog/script"
    if "skills/" in path or path.endswith("SKILL.md"):
        return "skill"
    if path.endswith("SOUL.md"):
        return "soul"
    if path.endswith(("config.yaml", "models.yaml", "jobs.json")):
        return "config"
    if "tests/" in path or "/test_" in path or path.startswith("test_"):
        return "test"
    return "other"


def commits(since_iso: str):
    raw = git("log", f"--since={since_iso}", "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s", "fleet")
    out = []
    for line in (raw or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        full, short, who, when, subj = parts
        files = (git("show", "--name-only", "--pretty=format:", full) or "").split()
        cats = {}
        for f in files:
            cats.setdefault(classify(f), []).append(f)
        out.append({"sha": short, "author": who, "at": when, "subject": subj,
                    "files": files, "cats": cats})
    return out


def manifests(cutoff: float):
    made, unapplied = [], []
    for p in sorted(CHANGES.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            continue
        created = d.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(created).timestamp() if created else 0
        except ValueError:
            ts = 0
        if ts >= cutoff:
            made.append({"id": d.get("id"), "title": d.get("title", ""),
                         "applied": bool(d.get("applied_at")), "restart": d.get("restart") or [],
                         "items": len(d.get("items") or [])})
            if not d.get("applied_at"):
                unapplied.append(d.get("id"))
    return made, unapplied


def _ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)


def cards(cutoff: float):
    """Cards that reached a terminal state in the window, with spend split PER WORKER."""
    if not DB.is_file():
        return [], []
    done, overwatch = [], []
    try:
        c = _ro(DB); c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, title, assignee, status, completed_at FROM tasks "
            "WHERE completed_at IS NOT NULL AND completed_at >= ?", (cutoff,)).fetchall()
        ledgers = [("root", HERMES_HOME / "state.db")] + [
            (p.parent.name, p) for p in sorted((HERMES_HOME / "profiles").glob("*/state.db"))]
        for r in rows:
            per = {}
            for prof, db in ledgers:
                if not db.exists():
                    continue
                try:
                    lc = _ro(db)
                    v = lc.execute(
                        "SELECT COALESCE(SUM(CASE WHEN COALESCE(actual_cost_usd,0)>0 "
                        "THEN actual_cost_usd ELSE COALESCE(estimated_cost_usd,0) END),0) "
                        "FROM sessions WHERE title LIKE ?", (f"%{r['id']}%",)).fetchone()[0]
                    lc.close()
                    if v and v > 0.005:
                        per[prof] = round(float(v), 4)
                except sqlite3.Error:
                    continue
            done.append({"id": r["id"], "title": (r["title"] or "")[:70], "status": r["status"],
                         "assignee": r["assignee"], "per_worker": per,
                         "over_cap": sorted(k for k, v in per.items() if v > 1.0)})
        for r in c.execute(
                "SELECT task_id, author, substr(body,1,90) AS b, created_at FROM task_comments "
                "WHERE body LIKE 'overwatch:%' AND created_at >= ? ORDER BY id", (cutoff,)):
            overwatch.append({"task": r[0], "author": r[1], "head": r[2]})
        c.close()
    except sqlite3.Error:
        pass
    return done, overwatch


def controls():
    out = {}
    try:
        out["preflight"] = json.loads((HERMES_HOME / "state/fleet-preflight.json").read_text())
    except Exception:  # noqa: BLE001
        out["preflight"] = None
    out["queue_failed"] = sorted(p.stem for p in (HERMES_HOME / "state/apply-queue").glob("*.failed"))
    out["queue_armed"] = []
    for p in sorted((HERMES_HOME / "scripts/apply-queue").glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            continue
        if d.get("armed") and not (HERMES_HOME / "state/apply-queue" / f"{d['id']}.done").exists():
            out["queue_armed"].append(d["id"])
    streaks = []
    try:
        j = json.loads((HERMES_HOME / "cron/jobs.json").read_text())
        for job in (j["jobs"] if isinstance(j, dict) and "jobs" in j else j):
            n = int(job.get("failure_streak") or 0)
            if n >= 3:
                streaks.append({"job": job.get("name"), "streak": n,
                                "last_error": (job.get("last_error") or "")[:120]})
    except Exception:  # noqa: BLE001
        pass
    out["failing_jobs"] = streaks
    return out


def main():
    now = time.time()
    cutoff = now - WINDOW_H * 3600
    since_iso = datetime.fromtimestamp(cutoff).isoformat(timespec="seconds")
    cs = commits(since_iso)
    made, unapplied = manifests(cutoff)
    done, ow = cards(cutoff)
    ctl = controls()

    cat_totals = {}
    kernel_hits, untested = [], []
    for c in cs:
        for k, v in c["cats"].items():
            cat_totals[k] = cat_totals.get(k, 0) + len(v)
        if "KERNEL" in c["cats"]:
            kernel_hits.append(c)
        # Calibrate, or this cries wolf and gets ignored — which is the failure mode it exists to
        # catch. Two exemptions, both earned: a dashboard change is gated by its own build script
        # (bundle symbols, CSS invariants, a live API assertion) and carries no pytest file by
        # design; and a commit that only MOVES files under version control adds no behaviour.
        code = [f for k in ("plugin", "watchdog/script") for f in c["cats"].get(k, [])]
        code = [f for f in code if "/dashboard/" not in f]
        moved_only = c["subject"].lower().startswith(("watchdogs: the rest", "chore: move", "move "))
        if code and "test" not in c["cats"] and not moved_only:
            untested.append(c)

    L = []
    A = L.append
    A(f"# Fleet self-change review — {datetime.fromtimestamp(now):%Y-%m-%d %H:%M} "
      f"(last {WINDOW_H:g}h)\n")
    A("*Evidence only — zero tokens, read-only. The judgement half is the Claude session that "
      "reads this. Numbers here are measurements; anything you conclude from them is not.*\n")

    A(f"## Changes to the substrate — {len(cs)} commit(s) on `fleet`\n")
    if not cs:
        A("Nothing was committed in this window.\n")
    else:
        A("| commit | by | extension points | files | subject |")
        A("|---|---|---|---|---|")
        for c in cs:
            pts = ", ".join(f"{k}×{len(v)}" for k, v in sorted(c["cats"].items()))
            A(f"| `{c['sha']}` | {c['author']} | {pts} | {len(c['files'])} | {c['subject'][:70]} |")
        A("")

    A("## The one thing that must never happen\n")
    if kernel_hits:
        A("🔴 **KERNEL PATHS WERE TOUCHED.** The 2026-09-09 rule says policy lives in plugins, "
          "watchdogs, skills, SOULs and config, and that a core patch is never the fallback.\n")
        for c in kernel_hits:
            A(f"- `{c['sha']}` {c['subject'][:80]} — {', '.join(c['cats']['KERNEL'])}")
        A("\nCheck for a `core-patch-approved` reason before treating this as a breach; if there "
          "is none, this is the batch-revert case from 2026-09-12.\n")
    else:
        A("✅ No commit touched `hermes_cli/`, `tools/`, `agent/` or `gateway/`.\n")

    A("## Quality signal — did code arrive with tests?\n")
    if untested:
        A("A plugin or watchdog changed with **no test file in the same commit**. Dashboard changes "
          "and pure file moves are exempt (the first has a build gate, the second adds no "
          "behaviour). Not automatically wrong, but it is the shape every phantom fix on this fleet "
          "has had, so it is the first place to look:\n")
        for c in untested:
            A(f"- `{c['sha']}` {c['subject'][:80]}")
        A("")
    else:
        A("✅ Every plugin/watchdog commit in this window carried a test file.\n")

    A(f"## Change manifests — {len(made)} created\n")
    if made:
        for m in made:
            A(f"- `{m['id']}` — {m['title'][:70]} · {m['items']} item(s) · "
              f"{'applied' if m['applied'] else '**NOT APPLIED**'}"
              + (f" · restart {m['restart']}" if m["restart"] else ""))
        A("")
    if unapplied:
        A(f"⚠️ **Unapplied manifests: {', '.join(unapplied)}.** An unapplied manifest is a change "
          f"that cannot be rolled back cleanly and that pre-flight will read as restart debt.\n")

    A(f"## Cards the fleet finished — {len(done)}\n")
    if done:
        A("Spend is split **per worker**, because the cap is per worker and a pooled total has read "
          "as a breach three times.\n")
        A("| card | assignee | per-worker spend | over own $1 |")
        A("|---|---|---|---|")
        for d in done:
            spend = " · ".join(f"{k} ${v:.2f}" for k, v in sorted(d["per_worker"].items())) or "—"
            A(f"| `{d['id']}` | {d['assignee']} | {spend} | "
              f"{', '.join(d['over_cap']) if d['over_cap'] else '—'} |")
        A("")

    A(f"## Overwatch interventions — {len(ow)}\n")
    if ow:
        authors = {}
        for o in ow:
            authors[o["author"]] = authors.get(o["author"], 0) + 1
        A(f"By stored author: {', '.join(f'{k} {v}' for k, v in sorted(authors.items()))}\n")
        strays = [a for a in authors if a not in ("default", "root")]
        if strays:
            A(f"🔴 **Attribution regression:** an `overwatch:` ruling is stored under {strays}. "
              f"Since 2026-09-15 the escalator sets `HERMES_PROFILE`/`HERMES_PROFILE_NAME` to the "
              f"assessor, so every one of these should read `default`. A stray name means that fix "
              f"regressed or something else is writing these comments.\n")
        for o in ow[:8]:
            A(f"- `{o['task']}` [{o['author']}] {o['head'][:80]}")
        A("")

    A("## The controls' own health\n")
    pf = ctl["preflight"]
    A(f"- pre-flight: **{(pf or {}).get('verdict', 'unknown')}**"
      + (f" — {pf.get('signature')}" if pf and pf.get("verdict") != "GREEN" else ""))
    A(f"- apply-queue: {len(ctl['queue_armed'])} armed and waiting"
      + (f", **PARKED on {', '.join(ctl['queue_failed'])}**" if ctl["queue_failed"] else ""))
    if ctl["failing_jobs"]:
        for j in ctl["failing_jobs"]:
            A(f"- 🔴 cron `{j['job']}` failure streak {j['streak']} — {j['last_error']}")
    else:
        A("- no cron job has a failure streak of 3 or more")
    A("")

    A("## For the reviewer\n")
    A("1. **Purpose** — for each commit, does what it did match what its card asked for?")
    A("2. **Compliance** — right extension point, manifest with a pre-image, tests with a negative "
      "control, no kernel path without approval?")
    A("3. **Adapt the work** — if a change is sound but non-compliant, fix it tonight rather than "
      "reverting it; if it is unsound, roll it back with its manifest.")
    A("4. **Adapt the system** — what would have made the fleet get this right unprompted? A SOUL "
      "line, a skill correction, a watchdog, a brief change. Prefer detection over prevention, and "
      "prefer correcting a false claim over adding a new one.")

    report = HERMES_HOME / "logs" / f"nightly-selfchange-{datetime.fromtimestamp(now):%Y%m%d}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(L) + "\n")

    flags = []
    if kernel_hits:
        flags.append(f"{len(kernel_hits)} KERNEL commit(s)")
    if unapplied:
        flags.append(f"{len(unapplied)} unapplied manifest(s)")
    if ctl["queue_failed"]:
        flags.append("apply-queue PARKED")
    if ctl["failing_jobs"]:
        flags.append(f"{len(ctl['failing_jobs'])} failing cron job(s)")
    if untested:
        flags.append(f"{len(untested)} untested code commit(s)")
    print(f"nightly-selfchange-review: {len(cs)} commit(s), {len(done)} card(s), "
          f"{len(made)} manifest(s) — {'; '.join(flags) if flags else 'no flags'}. {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
