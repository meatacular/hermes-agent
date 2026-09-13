#!/usr/bin/env python3
"""skill-hygiene-watch — per-skill defects the context budget cannot see.

`context-budget-watch` measures the skills index in aggregate. It cannot tell you that a
description says what a skill IS instead of when to reach for it, that a SKILL.md has grown
past what a worker can force-load, or that a skill is disabled on a lane where a card has
already named it — which crashes the worker at agent init.

The four checks, each from a defect measured on 2026-09-13:

1. DISABLED-BUT-ATTACHED. `build_preloaded_skills_prompt` treats a disabled skill as MISSING,
   while the dispatcher's `missing_skills_for` only walks the filesystem and says "allow". The
   card is claimed, the worker forks, and exits 1 at agent init. Twice. Six cards were lost that
   way on 2026-09-05. This is the one that costs money; it is reported first.
2. OVERSIZED. A skill attached via `skills:` is loaded WHOLE. kanban-ops was 46,003 B and
   kanban-implementation-orchestration 122,382 B before they were split.
3. DESCRIPTION WITHOUT A TRIGGER. 35 of 48 worker-facing descriptions described the artefact
   rather than when to use it; one was literally "|" — a broken YAML block rendering empty.
   The description is the only lever vendors name for selection accuracy.
4. DANGLING REFERENCE. A SKILL.md routing table pointing at a references/ file that is not
   there. A mention that names ANOTHER skill in the same breath resolves against that skill --
   the first live run flagged four cross-skill pointers that were perfectly valid, and a
   watchdog that cries wolf is a watchdog nobody reads.

Silent when clean. Read-only apart from its own state file. Zero tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
STATE = Path(os.environ.get("SKILL_HYGIENE_WATCH_STATE")
             or HERMES_HOME / "state" / "skill-hygiene-watch.json")

MAX_SKILL_BYTES = 15_000
SUPPORT_DIRS = {"references", "templates", "assets", "scripts"}
# A description that states WHEN to reach for the skill, rather than what it is.
TRIGGER = re.compile(
    r"\b(use when|use before|use this|use it when|use for|use to|"
    r"read first|read before|read this when|read it when|"
    r"when you|whenever|before you|before any|before \w+ing|"
    r"trigger|reach for|invoke when|call this when)\b",
    re.I)


# --------------------------------------------------------------------- pure logic

def has_trigger(description: str) -> bool:
    """True when the description tells the model WHEN to use the skill."""
    d = (description or "").strip()
    return bool(d) and len(d) > 12 and bool(TRIGGER.search(d))


def evaluate(profile, skills, disabled, attached, max_bytes=MAX_SKILL_BYTES):
    """Pure. skills: {name: {"bytes": int, "description": str, "dangling": [str]}}."""
    out = []
    for name in sorted(skills):
        s = skills[name]
        if name in disabled:
            if name in attached:
                out.append(("crash-risk", name,
                            "DISABLED here but a card has attached it — the dispatcher will allow "
                            "the card and the worker will exit 1 at agent init"))
            continue                      # a disabled skill costs nothing else
        if s.get("bytes", 0) > max_bytes:
            out.append(("oversized", name,
                        f"{s['bytes']} B > {max_bytes} — split into SKILL.md + references/"))
        if not has_trigger(s.get("description", "")):
            d = (s.get("description") or "").strip()
            out.append(("description", name,
                        "says what it is, not when to use it: "
                        + (repr(d[:70]) if d else "EMPTY")))
        for ref in s.get("dangling", []):
            out.append(("dangling-ref", name, f"routing table points at missing {ref}"))
    return out


def render(findings_by_profile):
    """Pure: {profile: [(kind, skill, detail)]} -> report text ('' when clean)."""
    hits = {p: f for p, f in findings_by_profile.items() if f}
    if not hits:
        return ""
    order = {"crash-risk": 0, "oversized": 1, "dangling-ref": 2, "description": 3}
    total = sum(len(f) for f in hits.values())
    lines = [f"🧹 skill-hygiene-watch: {total} finding(s) across {len(hits)} profile(s)"]
    crash = [(p, s, d) for p, f in hits.items() for k, s, d in f if k == "crash-risk"]
    if crash:
        lines += ["", "   ⚠️  CRASH RISK — a card naming these will burn its whole retry budget:"]
        lines += [f"      {p}/{s}: {d}" for p, s, d in crash]
    for p in sorted(hits):
        rest = sorted((k, s, d) for k, s, d in hits[p] if k != "crash-risk")
        if not rest:
            continue
        lines += ["", f"   {p}:"]
        lines += [f"      [{k}] {s} — {d}" for k, s, d in sorted(rest, key=lambda x: order[x[0]])]
    lines += ["", "   Procedure: the `skill-update` skill. Descriptions state WHEN to use, not what "
                  "the thing is; over 15 KB split into SKILL.md + references/; never disable a skill "
                  "any card has attached."]
    return "\n".join(lines)


def dangling_refs(body: str, own_present: set, refs_by_skill: dict) -> list:
    """Pure. references/X.md mentions in `body` that resolve to no file on disk.

    A mention qualified ON THE SAME LINE by another skill's name --
    `skill_view(name="kanban-ops", file_path="references/gridlock.md")`, an absolute path,
    or prose like "`hermes-agent` -> `references/background-systems.md`" -- resolves
    against THAT skill. Everything else resolves against the skill's own references/.

    Same line, not a character window: a window wide enough to catch the routing arrow also
    catches the name from the PREVIOUS mention, which hid a genuinely missing file. And the
    name match is whole-word, or a one-letter skill called `a` matches every line in the file.
    Both were caught by the test gate before this shipped.
    """
    out = set()
    matchers = {n: re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(n) + r"(?![A-Za-z0-9_-])")
                for n in refs_by_skill}
    for line in body.splitlines():
        for m in re.finditer(r"references/([A-Za-z0-9._-]+\.md)", line):
            fname = m.group(1)
            before = line[:m.start()]
            named = [n for n, rx in matchers.items() if rx.search(before)]
            if named:
                # longest name wins: weroll-knowledge-search over weroll-knowledge
                owner = max(named, key=len)
                if fname not in refs_by_skill[owner]:
                    out.add(fname)          # named skill really lacks it -- still a bad pointer
                continue
            if fname not in own_present:
                out.add(fname)
    return sorted(out)


# ------------------------------------------------------------------------ gather

def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.match(r"(?s)\A---\n(.*?)\n---", text)
    out = {"_body": text}
    if not m:
        return out
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if km:
            out[km.group(1)] = km.group(2).strip().strip("\"'")
    return out


def skills_of(home: Path) -> dict:
    root = home / "skills"
    out = {}
    if not root.is_dir():
        return out
    found = []
    for p in root.rglob("SKILL.md"):
        if SUPPORT_DIRS & set(p.parts):
            continue
        found.append(p)
    # Pass 1: every skill's references/ on this profile, so a cross-skill pointer can be resolved.
    refs_by_skill = {
        p.parent.name: ({f.name for f in (p.parent / "references").glob("*.md")}
                        if (p.parent / "references").is_dir() else set())
        for p in found
    }
    # Pass 2: measure.
    for p in found:
        fm = _frontmatter(p)
        body = fm.get("_body", "")
        present = refs_by_skill.get(p.parent.name, set())
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        out[p.parent.name] = {
            "bytes": size,
            "description": fm.get("description", ""),
            "dangling": dangling_refs(body, present, refs_by_skill),
        }
    return out


def disabled_of(home: Path) -> set:
    """`skills.disabled` from this profile's config.yaml, hand-parsed (no yaml dependency)."""
    try:
        lines = (home / "config.yaml").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    try:
        si = next(i for i, l in enumerate(lines) if l.rstrip() == "skills:")
    except StopIteration:
        return set()
    out, in_dis = set(), False
    for line in lines[si + 1:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        st = line.strip()
        if re.match(r"^disabled:\s*(\[\s*\])?\s*(#.*)?$", st):
            in_dis = True
            continue
        if in_dis:
            m = re.match(r"^-\s+(\S+)", st)
            if m:
                out.add(m.group(1).strip("\"'"))
            elif st and not st.startswith("#"):
                in_dis = False
    return out


def attached_skills(home: Path) -> set:
    """Every skill name any card has ever carried, from the board."""
    db = home / "kanban.db"
    if not db.exists():
        return set()
    out = set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for (raw,) in con.execute("select skills from tasks where skills is not null "
                                  "and skills not in ('','[]','null')"):
            try:
                out.update(json.loads(raw))
            except (ValueError, TypeError):
                continue
    except sqlite3.Error:
        return set()
    return {s for s in out if isinstance(s, str)}


def profiles(home: Path):
    yield "root", home
    pdir = home / "profiles"
    if pdir.is_dir():
        for d in sorted(pdir.iterdir()):
            if d.is_dir() and (d / "skills").is_dir():
                yield d.name, d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(HERMES_HOME))
    ap.add_argument("--no-state", action="store_true")
    ap.add_argument("--max-bytes", type=int, default=MAX_SKILL_BYTES)
    a = ap.parse_args()
    home = Path(a.home)

    attached = attached_skills(home)
    findings = {}
    for name, phome in profiles(home):
        findings[name] = evaluate(name, skills_of(phome), disabled_of(phome), attached,
                                  max_bytes=a.max_bytes)
    report = render(findings)

    fingerprint = {p: sorted(f"{k}:{s}" for k, s, _d in f) for p, f in findings.items() if f}
    if not a.no_state:
        try:
            prev = json.loads(STATE.read_text()).get("fingerprint", {})
        except (OSError, ValueError):
            prev = {}
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"at": int(time.time()), "fingerprint": fingerprint}))
        except OSError:
            pass
        if fingerprint and fingerprint == prev:
            return 0                                   # unchanged: stay quiet
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
