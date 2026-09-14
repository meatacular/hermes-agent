#!/usr/bin/env python3
"""job-complete-watch - charter §8 evidence bundle to Richie (build-list item 2).

When a PARENT card (one with children in task_links) reaches `done`, post the
evidence bundle to Slack and iMessage, once per parent:

  card id · each numbered criterion with pass/fail (from Jobsy's closing comment
  on the parent) · preview URL · screenshots (attachment names) · design path ·
  PR link · total cost against estimate · points against cycle time · Rodge's
  verdict · the held deploy card and how to release it.

It ASSEMBLES from what the agents wrote; it does not judge. If Jobsy's closing
comment is missing the bundle says so - a missing bundle is itself evidence.

**Changed 2026-09-14 (Richie): the message IS the human summary.** When
`~/Projects/hermes management/jobs/<parent>.md` exists, that file's content is
the message - sent whole when it fits `JOB_COMPLETE_HUMAN_MAX` (default 1600
chars), otherwise a succinct extract of its own headings (goal, cards, verifier
gates, top risk), plus the one-line deploy release. The assembled bundle above
is now only the fallback for a parent with no human summary.

Only parents completed after this watch was installed are reported (state has a
`since` watermark), so installing it does not replay 170 historical parents.
`no_agent`, read-only, zero LLM tokens, silent when nothing completed. Exit 0.
Test overrides: JOB_COMPLETE_DB, JOB_COMPLETE_STATE, JOB_COMPLETE_SINCE (epoch),
FLEET_NOTIFY_DRYRUN.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from fleet_notify import slack_dm
except Exception:  # noqa: BLE001
    def slack_dm(text, channel=None):  # type: ignore
        return False

try:  # comms-format-standard Rule 1: hyphens only in anything a human reads
    from comms_style import clean
except Exception:  # noqa: BLE001
    clean = None

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
DB = Path(os.environ.get("JOB_COMPLETE_DB") or HERMES_HOME / "kanban.db")
STATE = Path(os.environ.get("JOB_COMPLETE_STATE") or HERMES_HOME / "state" / "job-complete-watch.json")
LEDGER = HERMES_HOME / "logs" / "cost-ledger.jsonl"
JOBS_DIR = Path.home() / "Projects" / "hermes management" / "jobs"

PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
URL_RE = re.compile(r"https?://[^\s)>\]`'\"]+")
PREVIEW_HINTS = ("vercel.app", "preview", "localhost", "127.0.0.1", "ts.net", ".pages.dev")
DESIGN_RE = re.compile(r"design/[\w.-]+/")
EST_RE = re.compile(r"cost-estimate[^0-9$]*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
PTS_RE = re.compile(r"points-estimate[^0-9]*([0-9]+)", re.I)
AC_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:AC\s*)?(\d+)[.):]\s*(.+)$", re.I | re.M)


def _ledger():
    rows = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                r = json.loads(line)
                rows[r["card"]] = r
            except Exception:  # noqa: BLE001
                pass
    return rows


def _fmt_dur(s):
    s = int(s or 0)
    return f"{s//3600}h{(s%3600)//60:02d}m" if s >= 3600 else f"{s//60}m"


def _section(text, name):
    m = re.search(rf"^##\s*{re.escape(name)}[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    for sep in (". ", "; ", ", "):
        i = cut.rfind(sep)
        if i > n * 0.5:
            return cut[:i + 1] + " …"
    return cut.rstrip() + "…"


def render_human(path, pid, title, limit=None):
    """Richie 2026-09-14: the message IS the human summary. Sent whole when it is
    short enough; otherwise a succinct extract of its own headings (goal, cards,
    verifier gates, top risk). Deterministic - this stays a zero-token watchdog."""
    limit = limit or int(os.environ.get("JOB_COMPLETE_HUMAN_MAX") or 1600)
    text = path.read_text(errors="replace").strip()
    head = f"*Job complete* `{pid}` - {(title or '')[:90]}"
    if len(text) <= limit:
        return head + "\n" + text
    out = [head]
    goal = _clip(_section(text, "Goal"), 420)
    if goal:
        out.append("Goal: " + goal)
    steps = re.findall(r"^\s*\d+\.\s+\*\*(.+?)\*\*", _section(text, "Phase breakdown"), re.M)
    if steps:
        out.append("Cards: " + " · ".join(_clip(s, 70) for s in steps[:8]))
    acs = re.findall(r"^\s*[-*]\s*(AC\d+)[:\s]\s*(.+)$", _section(text, "Acceptance gates"), re.M | re.I)
    if acs:
        out.append("Gates: " + " · ".join(f"{n} {_clip(d, 90)}" for n, d in acs[:6]))
    risk = re.search(r"^\s*[-*]\s*(.+)$", _section(text, "Risks"), re.M)
    if risk:
        out.append("Risk: " + _clip(risk.group(1), 200))
    body = "\n".join(out)
    if len(body) > limit:
        body = body[:limit].rstrip() + " …"
    return body


def bundle(c, p, ledger):
    """Richie 2026-09-14: the message IS the human summary. A card with no human
    summary on file is not a job parent, so it gets a two-line notice - the old
    assembled evidence bundle is gone."""
    pid = p["id"]
    kids = c.execute(
        "SELECT t.* FROM tasks t JOIN task_links l ON l.child_id=t.id WHERE l.parent_id=? ORDER BY t.created_at",
        (pid,),
    ).fetchall()
    ids = [pid] + [k["id"] for k in kids]
    actual = sum((ledger.get(i) or {}).get("actual_usd", 0.0) for i in ids)
    cycle = (p["completed_at"] or time.time()) - (p["created_at"] or time.time())
    deploy = next((k for k in kids if (k["title"] or "").startswith("Deploy:")), None)
    title = (p["title"] or "")[:90]
    human = JOBS_DIR / f"{pid}.md"

    if human.exists():
        err = None
        try:
            rendered = render_human(human, pid, title)
        except Exception as e:  # noqa: BLE001
            rendered, err = None, e
        if rendered:
            if deploy:
                st = f"{deploy['status']}" + (f"/{deploy['block_kind']}" if deploy["block_kind"] else "")
                rendered += (f"\n\nRelease: deploy card `{deploy['id']}` is {st} - approve with "
                             f"`hermes kanban unblock {deploy['id']}`; request changes via Switch or Smith.")
            else:
                rendered += "\n\n⚠️ No `Deploy:` child card - nothing is held for your approval."
            return rendered
        note = f"  ⚠️ Human summary unreadable ({human.name}): {err} - nothing to report."
    else:
        note = (f"  No human summary on file ({human.name}) - not a job parent; "
                f"{len(kids)} child card(s), ${actual:.4f}, cycle {_fmt_dur(cycle)}.")

    out = [f"*Job complete* `{pid}` - {title}", note]
    if deploy:
        st = f"{deploy['status']}" + (f"/{deploy['block_kind']}" if deploy["block_kind"] else "")
        out.append(f"  Release: deploy card `{deploy['id']}` is {st} - "
                   f"`hermes kanban unblock {deploy['id']}`.")
    return "\n".join(out)


def main() -> int:
    if not DB.exists():
        return 0
    try:
        state = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        state = {}
    since = float(os.environ.get("JOB_COMPLETE_SINCE") or state.get("since") or 0)
    if not since:
        since = time.time()
        state["since"] = since  # watermark: do not replay history
    seen = state.setdefault("seen", {})
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        parents = c.execute(
            "SELECT * FROM tasks WHERE status='done' AND completed_at >= ? "
            "AND id IN (SELECT DISTINCT parent_id FROM task_links) ORDER BY completed_at",
            (since,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"job-complete-watch ERROR: {e}")
        return 0
    ledger = _ledger()
    out = []
    for p in parents:
        if p["id"] in seen:
            continue
        try:
            out.append(bundle(c, p, ledger))
        except Exception as e:  # noqa: BLE001
            out.append(f"*Job complete* `{p['id']}` - bundle assembly failed: {e}")
        seen[p["id"]] = int(time.time())
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE)
    except Exception:  # noqa: BLE001
        pass
    if out:
        text = "\n\n".join(out)
        if clean:
            text = clean(text)
        print(text)
        slack_dm(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
