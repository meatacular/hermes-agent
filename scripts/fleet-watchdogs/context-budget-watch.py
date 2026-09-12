#!/usr/bin/env python3
"""context-budget-watch — is any profile's authored context growing past its budget?

Measured 2026-09-13 (SOUL-REVIEW-2026-09-13.md): the fleet's SOULs went 38,473 ->
117,623 bytes in three weeks; root went 8,975 -> 30,597 chars in eleven days, and the
`soul-compact-20260909` pass returned 1,279 bytes which were erased within twelve hours.
Compaction was losing to accretion about 15:1 and nothing measured it.

This measures the FOUR slots the fleet owns and pays for on every card:

    SOUL.md | rendered skills index | memories/MEMORY.md | memories/USER.md

Budgeting the SOUL alone is the trap: cut every SOUL to 1,500 chars and change nothing
else and the skills index becomes 53-78% of what each worker reads. A green soul beside
a bloated index is a dumber agent and a watchdog that says GREEN.

Repo-supplied project context (.hermes.md / AGENTS.md / CLAUDE.md, resolved from the
card's WORKSPACE) is REPORTED BUT EXEMPT: hermes-agent's own AGENTS.md is 29,565 chars
and is correctly large for someone editing that codebase. We budget what we author.

It also reports HEADROOM to each profile's `context_file_max_chars` and DAYS-TO-BREACH
at the trailing growth rate taken from the SOUL's own `.bak-*` chain. A SOUL past that
cap is head/tail truncated with only a line in agent.log to say so -- that happened to
root at least 17 times from 2026-09-10 and nobody read it.

Silent when every profile is inside budget. Read-only apart from its own state file.
"""
from __future__ import annotations

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
STATE = Path(os.environ.get("CONTEXT_BUDGET_WATCH_STATE")
             or HERMES_HOME / "state" / "context-budget-watch.json")
VENV_PY = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python"
REPO = HERMES_HOME / "hermes-agent"

# Budgets in CHARACTERS. MEMORY and USER are the memory store's own truncation limits
# (tools/memory_tool.py: memory_char_limit / user_char_limit), not invented numbers --
# a file past them is already being cut before anyone reads it.
BUDGETS = {
    "worker": {"soul": 3000, "skills_index": 3000, "memory": 2200, "user": 1375, "total": 8000},
    "root":   {"soul": 12000, "skills_index": 6000, "memory": 2200, "user": 1375, "total": 15000},
}
SLOTS = ("soul", "skills_index", "memory", "user")

# Warn when a SOUL is projected to hit its context_file_max_chars within this many days.
BREACH_HORIZON_DAYS = 21
# Trailing window for the growth-rate fit, in days.
GROWTH_WINDOW_DAYS = 14


# --------------------------------------------------------------------------- pure logic

def growth_per_day(series):
    """Chars/day from a [(epoch_seconds, size_chars), ...] series, or None.

    Uses the oldest and newest points inside the window rather than a least-squares fit:
    the series is a backup chain, not a sample -- it is irregular, and one compaction
    dip would drag a fit below the real trend. Returns None on fewer than two usable
    points or a zero time span.
    """
    pts = sorted((t, s) for t, s in series if t and s)
    if len(pts) < 2:
        return None
    newest = pts[-1][0]
    window = [p for p in pts if newest - p[0] <= GROWTH_WINDOW_DAYS * 86400]
    if len(window) < 2:
        window = pts[-2:]
    (t0, s0), (t1, s1) = window[0], window[-1]
    days = (t1 - t0) / 86400.0
    return None if days <= 0 else (s1 - s0) / days


def days_to_breach(size, cap, rate):
    """Days until *size* reaches *cap* at *rate* chars/day; None when not applicable."""
    if not cap or rate is None or rate <= 0:
        return None
    return max(0.0, (cap - size) / rate)


def evaluate(profile, m, budgets=None):
    """Pure: measurements dict -> list of finding strings. Empty list == green."""
    budgets = budgets or BUDGETS
    b = budgets["root" if profile == "root" else "worker"]
    out = []
    owned = sum(int(m.get(s) or 0) for s in SLOTS)
    for slot in SLOTS:
        got, cap = int(m.get(slot) or 0), b[slot]
        if got > cap:
            out.append(f"{slot} {got} > {cap} (+{got - cap})")
    if owned > b["total"]:
        out.append(f"TOTAL owned {owned} > {b['total']} (+{owned - b['total']})")
    d = m.get("days_to_breach")
    if d is not None and d <= BREACH_HORIZON_DAYS:
        out.append(f"SOUL projected to pass context_file_max_chars={m.get('ctx_cap')} "
                   f"in {d:.1f} days at {m.get('growth_per_day', 0):.0f} chars/day")
    if m.get("soul_truncated"):
        out.append(f"SOUL IS TRUNCATED NOW: {m.get('soul')} chars vs cap {m.get('ctx_cap')}")
    return out


def render(results, budgets=None):
    """Pure: {profile: (measurements, findings)} -> report text ('' when all green)."""
    breaching = {p: (m, f) for p, (m, f) in results.items() if f}
    if not breaching:
        return ""
    lines = [f"📏 context-budget-watch: {len(breaching)} of {len(results)} profiles over budget",
             "   Slots are SOUL / skills index / MEMORY.md / USER.md — the context the fleet authors "
             "and pays for on every card. Repo project context is reported, not budgeted.",
             ""]
    head = f"   {'profile':10s} {'SOUL':>7s} {'index':>7s} {'MEM':>6s} {'USER':>6s} {'owned':>7s} {'proj ctx':>9s}"
    lines.append(head)
    for p, (m, _f) in sorted(results.items()):
        owned = sum(int(m.get(s) or 0) for s in SLOTS)
        proj = m.get("project_context")
        lines.append(f"   {p:10s} {int(m.get('soul') or 0):7d} {int(m.get('skills_index') or 0):7d} "
                     f"{int(m.get('memory') or 0):6d} {int(m.get('user') or 0):6d} {owned:7d} "
                     f"{('-' if not proj else str(proj)):>9s}")
    lines.append("")
    for p, (_m, f) in sorted(breaching.items()):
        lines.append(f"   {p}:")
        for item in f:
            lines.append(f"      - {item}")
    lines.append("")
    lines.append("   Destinations when trimming (SOUL-REVIEW-2026-09-13.md): identity/invariant -> SOUL; "
                 "job-conditional procedure -> a skill attached per card via `skills:`; repo-specific "
                 "operational rules -> .hermes.md in that repo; fleet-wide invariant -> a "
                 "plugin-rendered prompt section; must-not-be-violated -> plugin or watchdog; "
                 "dated narrative -> the management folder, not the prompt.")
    return "\n".join(lines)


# ------------------------------------------------------------------------------- measure

def _chars(path):
    try:
        return len(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return 0


def _bak_series(soul_path):
    """[(mtime_epoch, chars)] for a SOUL and its .bak-* siblings."""
    soul_path = Path(soul_path)
    series = []
    for p in [soul_path, *soul_path.parent.glob(soul_path.name + ".bak-*")]:
        try:
            series.append((int(p.stat().st_mtime), _chars(p)))
        except OSError:
            continue
    return series


def _probe(home, code, timeout=90):
    """Run *code* under the hermes venv with HERMES_HOME=home; stdout stripped, or ''."""
    if not VENV_PY.exists():
        return ""
    env = dict(os.environ, HERMES_HOME=str(home))
    try:
        r = subprocess.run([str(VENV_PY), "-c", code], env=env, cwd=str(REPO),
                           capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip()


_PROBE_CODE = (
    "import json, os, pathlib\n"
    "from agent.prompt_builder import build_skills_system_prompt, load_soul_md, "
    "_get_context_file_max_chars\n"
    "from hermes_cli.config import load_config_readonly\n"
    "home = pathlib.Path(os.environ['HERMES_HOME'])\n"
    "cfg = load_config_readonly() or {}\n"
    "ts = set(cfg.get('toolsets') or [])\n"
    "try:\n"
    "    idx = build_skills_system_prompt(available_tools={'skills_list','skill_view','skill_manage'},\n"
    "                                     available_toolsets=ts,\n"
    "                                     skills_dir_override=home/'skills') or ''\n"
    "except Exception:\n"
    "    idx = ''\n"
    "s = load_soul_md() or ''\n"
    "print(json.dumps({'skills_index': len(idx), 'ctx_cap': _get_context_file_max_chars(),\n"
    "                  'soul_truncated': '[...truncated' in s}))\n"
)


def measure(home, profile):
    """Measure one profile. Subprocess probe failures degrade to a file-size-only read."""
    home = Path(home)
    m = {
        "soul": _chars(home / "SOUL.md"),
        "memory": _chars(home / "memories" / "MEMORY.md"),
        "user": _chars(home / "memories" / "USER.md"),
        "skills_index": 0,
        "ctx_cap": None,
        "soul_truncated": False,
        "project_context": None,
    }
    probed = _probe(home, _PROBE_CODE)
    if probed:
        try:
            m.update(json.loads(probed.splitlines()[-1]))
        except (ValueError, IndexError):
            pass
    rate = growth_per_day(_bak_series(home / "SOUL.md"))
    m["growth_per_day"] = rate
    m["days_to_breach"] = days_to_breach(m["soul"], m.get("ctx_cap"), rate)
    return m


def profiles(home):
    yield "root", Path(home)
    pdir = Path(home) / "profiles"
    if pdir.is_dir():
        for d in sorted(pdir.iterdir()):
            if d.is_dir() and (d / "SOUL.md").exists():
                yield d.name, d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(HERMES_HOME))
    ap.add_argument("--no-state", action="store_true")
    ap.add_argument("--always-report", action="store_true",
                    help="print the table even when every profile is green")
    a = ap.parse_args()

    results = {}
    for name, home in profiles(a.home):
        m = measure(home, name)
        results[name] = (m, evaluate(name, m))
    if not results:
        return 0

    report = render(results)
    if a.always_report and not report:
        report = "📏 context-budget-watch: all profiles inside budget.\n" + render(
            {k: (v[0], ["(forced)"]) for k, v in results.items()}).split("\n", 1)[-1]

    # Dedupe: an unchanged set of findings is not re-reported every six hours.
    fingerprint = {p: sorted(f) for p, (_m, f) in results.items() if f}
    if not a.no_state:
        prev = {}
        try:
            prev = json.loads(STATE.read_text()).get("fingerprint", {})
        except (OSError, ValueError):
            prev = {}
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"at": int(time.time()), "fingerprint": fingerprint}))
        except OSError:
            pass
        if fingerprint and fingerprint == prev and not a.always_report:
            return 0                                   # unchanged breach: stay quiet

    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
