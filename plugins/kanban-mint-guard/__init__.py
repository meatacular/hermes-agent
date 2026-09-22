"""Mint-time assignee routing guard, on upstream's ``pre_tool_call`` hook.

See plugin.yaml for why this is a plugin and not the reverted core patch.

Contract (hermes_cli/plugins.py):
    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

Imports NOTHING from hermes_cli. It decides on the tool arguments alone, so it has
no merge surface at all and cannot be broken by upstream moving a symbol.
"""
from __future__ import annotations

import json
import logging
import os
import datetime
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["register", "on_pre_tool_call", "verdict", "declared_extension_point",
           "branch_citations", "worktree_base_conflict", "dir_workspace_conflict",
           "tenant_project_conflict", "branch_target_injection", "land_card_conflict",
           "repo_tenant_for", "bound_project_for", "in_worktree_scope"]

logger = logging.getLogger(__name__)

# Profiles that are not board lanes. A build/review/verify/deploy card must never
# land on one of these. (axel: WeRoll business only; switch: switchboard; brain:
# takes no cards — it has no kanban toolset.)
NON_LANE = frozenset({"axel", "switch", "brain"})

# Lane -> the profile that owns it.
LANE_OWNER = {"build": "bob", "review": "rodge", "verify": "steve-o",
              "design": "karl", "deploy": "default", "pm": "jobsy"}

# Title verbs -> lane. Ordered: the first match wins, so the more specific
# review/verify verbs are tested before the broad build verbs.
LANE_PATTERNS = (
    ("review", re.compile(r"^\s*(\[rodge\]|rodge\s*[—–-]|re-?review\b|review\b)", re.I)),
    ("verify", re.compile(r"^\s*(\[steve-?o\]|steve-?o\s*[—–-]|qa\b|verify\b|re-?verify\b|real-click\b)", re.I)),
    ("deploy", re.compile(r"^\s*(\[deploy\]|deploy\b|ship\b|release\b|roll\s*out\b)", re.I)),
    ("design", re.compile(r"^\s*(\[karl\]|karl\s*[—–-]|spec\b|design\b)", re.I)),
    ("pm",     re.compile(r"^\s*(\[jobsy\]|jobsy\s*[—–-]|decompose\b|triage\b)", re.I)),
    # Verbs kept in sync with _BUILD_LANE_VERBS below (same order, same words) — see the
    # "TWO LISTS" note on EDIT_VERB_LINE for why they are separate regexes.
    ("build",  re.compile(r"^\s*(\[bob\]|bob\s*[—–-]|build\b|implement\b|fix\b|patch\b|repair\b|rework\b|plumb\b|add\b|create\b|restore\b|scaffold\b|migrate\b)", re.I)),
)

# An explicit owner marker anywhere at the start: "[Bob] ..." or "Bob — ...".
OWNER_MARKER = re.compile(r"^\s*(?:\[(?P<b>[a-z][a-z0-9._-]{1,20})\]|(?P<c>[a-z][a-z0-9._-]{1,20})\s*[—–]\s)", re.I)

KNOWN = frozenset({"bob", "rodge", "steve-o", "karl", "jobsy", "default", "axel", "switch", "brain"})

# --- kernel-patch rule (2026-09-12) -----------------------------------------
# Upstream's kanban is the SUBSTRATE. A card may not propose editing it.
#
# This rule is DELIBERATELY NARROW, and that is a design decision, not laziness.
# Calibrating against the real bodies of 2026-09-12 showed prose cannot be
# classified safely: the batch's ANCHOR card quotes `hermes_cli/kanban_decompose.py
# ... rewrites only null/unknown` as a statement of existing behaviour, and another
# card says `Both touch hermes_cli/kanban_db.py` about two OTHER cards. Blocking on
# "a kernel path appears" would refuse both, and a guard that refuses correct cards
# stops the board — worse than the defect.
#
# So this blocks only the unambiguous shape: a line that STARTS with an edit verb
# and names a kernel path. Everything subtler is the job of
# scripts/fleet-watchdogs/core-patch-watch.py, which reads COMMITS — ground truth,
# no prose — and therefore catches what this cannot.
KERNEL_DIRS = ("hermes_cli", "tools", "agent", "gateway")
KERNEL_PATH = re.compile(
    r"(?<![\w/])"                                                # not mid-path: a BackupBrain card
                                                                 # saying backend/app/tools/x.py is
                                                                 # not a kernel edit
    r"(?:(?:" + "|".join(KERNEL_DIRS) + r")/[\w./-]*\.py"        # hermes_cli/kanban_db.py
    r"|kanban_(?:db|tools|decompose|db_graph|db_dispatch)\w*\.py)",  # or the bare module
    re.I)
# 2026-09-14 (runfix-20260914): the verb list was the hole, and it was a hole of the
# fleet's own making. Until today this read
# ``(patch|edit|modify|change|refactor|amend|update|revert)`` while LANE_PATTERNS["build"]
# — forty lines above, in this same file — already knew that ``fix``, ``add``, ``create``,
# ``implement``, ``plumb``, ``repair``, ``restore``, ``scaffold`` and ``migrate`` are the
# words people actually use for "write this code". Card t_dcaf62c1 said "**Fix** the
# decomposer" and "**Add** creation-time mapping in `kanban_db.py`"; neither verb was in
# the kernel list, so the guard passed it and a kernel patch went in unreviewed.
#
# TWO LISTS, ONE OF WHICH MUST BE A SUPERSET. They are deliberately NOT merged: widening
# the LANE regex would re-classify ordinary cards ("Update the runbook" is not a build
# card), which changes assignee routing. The invariant that matters is one-directional —
# every build-lane verb must also be a kernel-edit verb, or a card can name the build lane
# and edit the kernel without this rule seeing it. ``test_kernel_verbs_cover_build_lane``
# pins exactly that, so the two can never drift apart in the dangerous direction again.
_BUILD_LANE_VERBS = ("build", "implement", "fix", "patch", "repair", "rework",
                     "plumb", "add", "create", "restore", "scaffold", "migrate")
_KERNEL_ONLY_VERBS = ("edit", "modify", "change", "refactor", "amend", "update",
                      "revert", "rewrite", "wire", "introduce", "delete", "remove")
EDIT_VERBS = tuple(dict.fromkeys(_BUILD_LANE_VERBS + _KERNEL_ONLY_VERBS))
EDIT_VERB_LINE = re.compile(
    r"^\s*(?:[-*+]\s*|\d+[.)]\s*|#+\s*)?(?:\*\*)?"
    r"(" + "|".join(EDIT_VERBS) + r")\b", re.I)
NEGATION = re.compile(r"\b(do not|don'?t|never|must not|without|rather than|instead of|no code lands)\b", re.I)
CORE_OVERRIDE = "core-patch-approved:"
# The escape hatch must name WHO approved the change. Until 2026-09-16 the test was a bare
# substring, so a body that merely QUOTED the placeholder — `core-patch-approved: <who>`, which
# is how this rule's refusal message and its own card write it — switched BOTH this rule and the
# extension-point rule off. Measured on t_05a5883c: the card documenting the hatch was the card
# that bypassed it. A placeholder is not an approval; a name is.
_CORE_APPROVAL = re.compile(r"core-patch-approved:[ \t]*(?P<who>[^\s`<>{}]+)", re.I)


def _core_approved(body: str) -> bool:
    m = _CORE_APPROVAL.search(body)
    return bool(m and m.group("who") and not _PLACEHOLDER.match(m.group("who")))


def kernel_edit_line(body: str) -> Optional[str]:
    """The first line that plainly instructs a kernel edit, or None."""
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or not EDIT_VERB_LINE.match(line):
            continue
        if NEGATION.search(line):
            continue                       # "Do not modify tools/kanban_tools.py"
        m = KERNEL_PATH.search(line)
        if m:
            return f"{m.group(0)} (\"{line[:90]}\")"
    return None


CROSS_LANE_BLOCK = frozenset({"rodge", "steve-o", "karl"})   # a build card on one of these
OVERRIDE = "assignee-override:"


# --- extension-point ladder (2026-09-16, ladder-20260916) --------------------
# The fifth rule, and the only one that reads a marker the AUTHOR writes on purpose.
#
# The ladder — plugin -> watchdog -> skill -> soul -> config, and "if none of the five
# fits, hand it to Richie; a core patch is never the fallback" — lived only in SOUL prose,
# so a card could declare a seam that is not on the ladder and be created, decomposed and
# dispatched like any other. t_331cb549 declared `extension-point: config/kernel` and named
# hermes_cli/kanban_db.py as its deliverable; it was created, held, released, dispatched,
# and only the worker's judgement declined the kernel shape.
#
# WHY NOT WIDEN THE PROSE RULE INSTEAD (see the note on kernel_edit_line): prose cannot be
# classified safely. `extension-point: <value>` is a DELIBERATE marker, the same class of
# mechanism as `core-patch-approved:` and `assignee-override:` — reading it needs no
# judgement about English.
#
# NARROW, in the same way and for the same reason, but not so narrow that it misses the
# card that motivated it. A marker is read in exactly two forms:
#   (a) DECLARED at the start of a line (bullet / heading / bold / backticks allowed);
#   (b) introduced by a short parenthesised or bracketed label, e.g. ``Fix (extension-point: x)``.
# A bare mention inside a sentence, including inline code in prose, is evidence ABOUT the marker
# and is deliberately not read. Fenced blocks and block quotes are evidence too. This keeps the
# guard from refusing its own documentation while catching deliberate heading declarations.
#
# FAIL-OPEN ON ABSENCE: a body with no marker behaves exactly as it did before, so no
# existing mint path starts failing.
EXTENSION_POINTS = ("plugin", "watchdog", "skill", "soul", "config")
# The value is the FIRST TOKEN after the colon, so a trailing justification on the same line
# ("extension-point: plugin — a pre_tool_call hook") is not read as part of the value.
EXTENSION_MARKER_LINE = re.compile(
    r"^[ \t]*(?:[-*+>][ \t]+|\d+[.)][ \t]+|#{1,6}[ \t]+)*"     # bullet / quote / heading
    r"(?:[*_]{0,2})(?:`)?extension[-_ ]point(?:`)?(?:[*_]{0,2})[ \t]*[:=][ \t]*"
    r"(?P<v>[^\n]+?)[ \t]*$",
    re.I | re.M)
# At most four words in the label before the opening delimiter. Requiring the delimiter before
# the marker prevents a long prose sentence from qualifying as a declaration.
EXTENSION_MARKER_PAREN = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?(?:[*_]{0,2}[A-Za-z0-9][\w-]*[*_]{0,2}[ \t]+){0,3}"
    r"(?:\([^\n()]{0,80}|\[[^\n\[\]]{0,80})[ \t]*"
    r"extension[-_ ]point[ \t]*[:=][ \t]*(?P<v>[^\n)\]]+)", re.I)
EXTENSION_MARKER_CODE = re.compile(
    r"`[ \t]*extension[-_ ]point[ \t]*[:=][ \t]*(?P<v>[^`\n]+?)[ \t]*`", re.I)
# A quoted PLACEHOLDER is an illustration, not a declaration: `extension-point: <value>` is
# how the ladder itself is written down, and refusing that would refuse the documentation.
_PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|\.\.\.|…|x)$", re.I)
# One ladder value, or several joined by a separator ("soul or skill", "plugin|watchdog").
# The multi-value form is accepted on purpose: self-improvement-review.py already asks for
# `soul-or-skill`, and a card choosing between two SANCTIONED rungs is not the defect. Every
# part must still be on the ladder — which is what makes `config/kernel` a refusal.
_LADDER_SEP = re.compile(r"[-_ ]or[-_ ]|[/|,+]", re.I)


def _text(body: Any) -> str:
    """The body as text. Real rows in kanban.db hold a few BLOB bodies, and a guard that
    raises on one of those would fail OPEN at the hook — i.e. silently stop guarding."""
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray)):
        try:
            return bytes(body).decode("utf-8", "replace")
        except Exception:              # noqa: BLE001
            return ""
    return ""


def _marker_values(body: Any):
    """Every ``(start, value)`` marker candidate in *body*, in document order."""
    text = _text(body)
    hits = []
    lines = list(re.finditer(r"(?m)^.*(?:\n|$)", text))
    fence_positions = [i for i, m in enumerate(lines)
                       if re.match(r"^[ \t]*```", m.group(0).rstrip("\n"))]
    paired_fences = set(fence_positions) if len(fence_positions) % 2 == 0 else set(fence_positions[:-1])
    unmatched_fence = fence_positions[-1] if len(fence_positions) % 2 else None
    fenced = False
    for index, match in enumerate(lines):
        if unmatched_fence is not None and index > unmatched_fence:
            continue

        line_start, raw = match.start(), match.group(0)
        line = raw.rstrip("\n")
        if index in paired_fences:
            fenced = not fenced
            continue
        if fenced or re.match(r"^[ \t]*>", line):
            continue
        m = EXTENSION_MARKER_LINE.match(line) or EXTENSION_MARKER_PAREN.match(line)
        if m:
            hits.append((line_start + m.start(), m.group("v")))
            continue
        # Preserve the calibrated labelled form ("Held: ... `extension-point: x`").
        # A sentence that merely mentions the marker has no short label prefix and stays prose.
        if re.match(r"^[ \t]*(?:#{1,6}[ \t]+)?[A-Za-z][\w -]{0,24}:[ \t]+", line):
            m = re.search(r"`[ \t]*extension[-_ ]point[ \t]*[:=][ \t]*(?P<v>[^`\n]+?)[ \t]*`", line, re.I)
            if m:
                hits.append((line_start + m.start(), m.group("v")))
                continue
        # A code span at the start of a line is a deliberate declaration.
        m = EXTENSION_MARKER_CODE.match(line.lstrip())
        if m and line.lstrip().startswith("`"):
            hits.append((line_start + (len(line) - len(line.lstrip())), m.group("v")))
            continue
        # Preserve deliberate inline declarations introduced by a short label. This catches
        # ``**Held deliberately** (`operator_hold`) ... `extension-point: none of the five` ``
        # without mining arbitrary prose examples. The label must be at the start of the line,
        # and the marker must be a code span; a sentence mentioning the marker remains evidence.
        m = re.search(r"`[ \t]*extension[-_ ]point[ \t]*[:=][ \t]*(?P<v>[^`\n]+?)[ \t]*`", line, re.I)
        if m:
            prefix = line[:m.start()].strip()
            prefix = re.sub(r"^(?:[-*+][ \t]+|#{1,6}[ \t]+)?", "", prefix)
            label = prefix.strip()
            if re.match(r"^(?:\*\*)?(?:Held|Fix|Where|Extension point)\b", label, re.I):
                hits.append((line_start + m.start(), m.group("v")))
                continue
    hits.sort(key=lambda h: h[0])
    return [v for _, v in hits]


def _declared_value(raw: str) -> str:
    """The declared value from a marker's line remainder, or "" when there is none.

    The backtick case is the one real-world calibration bought: the marker is often written
    INSIDE a code span, and a card that DEFINES the marker writes

        - `extension-point:` declared, with the chosen seam justified against the alternatives.

    Here the backtick after the colon closes an OUTER span, so the trailing prose is the
    sentence, not the value. When the remainder starts with a backtick:
      * a second backtick later  -> the value is what sits between them
        (`extension-point: `kanban` core ...` -> "kanban");
      * no second backtick       -> nothing was declared on this line, skip it.
    Two real cards (t_03560fba, t_da58d620) define the marker exactly this way; reading the
    prose as the value refused both, which is the false positive this rule least affords.
    """
    s = (raw or "").lstrip()
    if s.startswith("`"):
        close = s.find("`", 1)
        if close < 0:
            return ""
        s = s[1:close]
    # The first token that is not pure markup: "** `plugin`" is a bold-wrapped label, and its
    # value is the second token, not "**".
    for piece in s.split():
        piece = piece.strip("`*_\"'")
        if piece:
            return piece.rstrip(".,;:)").strip()
    return ""


def declared_extension_point(body: Any) -> Optional[str]:
    """The first OFF-LADDER declared ``extension-point:`` value in *body*, or None.

    A ladder value, a quoted placeholder and an absent marker all return None.
    """
    for raw in _marker_values(body):
        token = _declared_value(raw)
        if not token or _PLACEHOLDER.match(token):
            continue
        parts = [p.strip("`*_\"'") for p in _LADDER_SEP.split(token) if p.strip()]
        if parts and all(p.lower() in EXTENSION_POINTS for p in parts):
            continue                       # a declared, sanctioned seam — not our business
        return token
    return None


TOPIC_TAG = re.compile(r"^\s*\[(?P<t>[a-z][a-z0-9 ._-]{1,20})\]\s*", re.I)


def _strip_topic_tag(title: str) -> str:
    """Drop a leading "[platform]"-style TOPIC tag so the lane verb after it is read.

    Only a tag that is NOT a profile name is stripped — "[Bob]" is an owner marker
    and must survive. Getting this wrong is what let
    "[platform] Plumb BACKUPBRAIN_API_KEY ..." -> axel through in the first draft;
    the negative control caught it.
    """
    m = TOPIC_TAG.match(title or "")
    if m and m.group("t").strip().lower() not in KNOWN:
        return (title or "")[m.end():]
    return title or ""


def _lane(title: str) -> Optional[str]:
    probe = _strip_topic_tag(title)
    for lane, pat in LANE_PATTERNS:
        if pat.match(title or "") or pat.match(probe):
            return lane
    return None


def _marker_owner(title: str) -> Optional[str]:
    m = OWNER_MARKER.match(title or "")
    if not m:
        return None
    name = (m.group("b") or m.group("c") or "").lower()
    return name if name in KNOWN else None      # "[platform]" is not an owner


# --- phantom assignee (2026-09-14, applyq-002) -------------------------------
# The narrow import is deliberate: ONE small, long-stable public function, the same shape the
# completion gate uses, and it means the persona aliases (smith -> default) resolve from their
# single source of truth instead of a second list here that would drift. Lazy and
# exception-guarded -- a guard must never become a crash surface, so if the profile layer cannot
# be read the answer is "known" and the card passes.
def _assignee_is_phantom(assignee) -> bool:
    """True only when *assignee* is set and provably names no profile."""
    name = str(assignee or "").strip()
    if not name:
        return False                       # unassigned is a different rule's problem
    try:
        from hermes_cli.profiles import profile_exists
        return not profile_exists(name)
    except Exception:                      # noqa: BLE001 -- fail OPEN, never block on our own error
        return False


# --- worktree base (2026-09-16, card t_305e022d) -----------------------------
# A `worktree` card's base is the primary repo's CURRENT HEAD. Nothing in the create path
# accepts a base ref — `hermes_cli/kanban_db_workspace._ensure_git_worktree()` runs
# `git worktree add -b <branch> <target> HEAD` and `grep base_ref|base_branch|start_point`
# over kanban_db_workspace.py / kanban_db.py / tools/kanban_tools_schemas.py is empty. So a card
# whose work must sit on a sibling branch lands in a tree cut from HEAD: the file it was told to
# edit is absent, or it edits a branch nobody will merge, and NOTHING ERRORS. Measured on
# 2026-09-16 — four cards found by hand (t_75ebec63 on `main` needing PR #48's branch;
# t_c827851e needing backend/app/orgagent/briefing.py, absent from `main` entirely; t_331cb549
# flagged by the PM; t_d5cdd9f2 whose ACs named a file not on its own branch, making them
# unsatisfiable by construction). Each cost a stalled card, a hand diagnosis and a body edit.
#
# The remedy is a different WORKSPACE KIND, not a patch: `workspace_kind='dir'` pointed at an
# existing worktree that already carries the target branch. This rule refuses the mint and says so.
#
# WHY THE CITATION READER IS NARROW (measured, not guessed — the same reason the kernel rule is
# narrow). Read naively — "does an existing branch name appear in the body?" — the rule refuses
# 90 of the 372 real worktree cards on this board, because `main` is both a branch and an English
# word: "`<main>` element", "do not push to `main`", "base `main` @ 8000d7a". Prose cannot be
# classified safely, so this reads only two shapes:
#   (a) DECLARED  — a line where the author names the branch: `Branch: wt/t_296c6855`,
#                   `base: origin/main`, `Base branch: design-system-adoption`. A deliberate
#                   marker, the same class of mechanism as `extension-point:`.
#   (b) DIRECTIVE — a line putting the work ON a branch: "Work on `backupbrain/wp5-...`",
#                   "Commit on `wt/t_296c6855`", "the briefing generation lives on `...`".
# Measured through the REAL hook chain over all 372 worktree rows (evidence_wt_base.txt,
# 2026-09-16): 44 refused, none of them for the shapes above — 17 against the card's own project
# repo (14 in backupbrain @ `main`, 3 in hermes-agent @ `fleet`: three cards committing onto one
# hand-cut branch, six WP5 cards on one consolidated branch, cards based on an unmerged branch),
# and 27 whose stored `workspace_path` names the legacy checkout now parked on `review-wp10`
# while the body declares `main`/`master` — the guard answers about the args it is given, and the
# message names the repo it resolved. dir (292) and scratch (302) rows replayed: rule 5 fired on
# none of them. The residual false positive — a card that DECLARES the branch it will itself
# CREATE — is documented and pinned in the test file; the remedy is one line of body text, and
# the message says which.
#
# FAIL-OPEN, like every other rule here: no marker, no readable body, no resolvable repo, a
# detached HEAD, a project we cannot resolve, or any exception at all -> the card is created.
WORKTREE_REASON_PREFIX = "worktree base"
# The markers an author uses to name the branch the work sits on. `base` alone is included
# because "base: origin/main" is the fleet's own idiom; a value that is not branch-shaped
# (`BASE=$(git merge-base ...)`, `BASE=http://localhost:$PORT`) is rejected by _REF_SHAPED.
WORKTREE_BASE_MARKERS = (r"base[-_ ]?branch", r"base[-_ ]?ref", r"target[-_ ]?branch",
                         r"branch", r"base")
WORKTREE_MARKER_LINE = re.compile(
    r"^[ \t]*(?:[-*+>][ \t]+|\d+[.)][ \t]+|#{1,6}[ \t]+)*"          # bullet / quote / heading
    r"(?:[*_]{0,2})(?:`)?(?:"
    + "|".join(WORKTREE_BASE_MARKERS) +
    r")(?:`)?(?:[*_]{0,2})[ \t]*[:=][ \t]*(?P<v>[^\n]+?)[ \t]*$",
    re.I)
# Verbs that put work ONTO a branch, plus the locative connectors. Deliberately NOT the delivery
# connectors (`to`, `into`, `from`, `off`): "push **`backupbrain/wp7-...`**", "merge branch X
# into Y", "cherry-pick from branch Z" are delivery/source statements on cards that are correct
# as worktree cards — measured; they were 5 of the 11 refusals of a wider draft.
WORKTREE_WORK_VERBS = (r"commit|commits|work|works|deliver|delivered|land|lands|landed|based|"
                       r"base|branch|branched|cut|lives|live|lived|resides|reside|exists|exist")
WORKTREE_DIRECTIVE = re.compile(
    r"\b(" + WORKTREE_WORK_VERBS + r")\b[^\n]{0,40}?\b(on|onto)\b[ \t]*"
    r"`?(?P<v>[A-Za-z0-9][\w./-]{1,80})`?", re.I)
# A branch name as git accepts it. Anything with a `$`, `=`, `(`, `:` or a space is a shell line,
# a URL or prose, not a ref.
_REF_SHAPED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
# A namespaced branch (`backupbrain/wp5-x`, `wt/t_296c6855`, `design-system-adoption`) is read as
# a branch even when no such ref exists yet; a bare single word is only read when it IS a ref, so
# `base: the parent commit` cannot refuse a card.
_REF_SEPARATOR = re.compile(r"[/\-_.]")
_WORKTREE_PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|\.\.\.|…|x|tbd|n/?a|none|same|new|-)$", re.I)


def _bare_ref(token: str) -> str:
    """`origin/main` -> `main`. A remote-tracking ref names the same branch."""
    t = (token or "").strip().strip("`*_\"'")
    for prefix in ("origin/", "refs/heads/"):
        if t.startswith(prefix):
            t = t[len(prefix):]
    return t


def branch_citations(body: Any):
    """Every ``(token, kind, line)`` branch citation in *body*, in document order.

    ``kind`` is ``"declared"`` (the author named the branch on its own line) or ``"directive"``
    (a line that puts the work ON a branch). Pure: no filesystem, no git — the caller decides
    which of these actually name a ref of the repo the card will be provisioned in.
    """
    out = []
    for raw in _text(body).splitlines():
        line = raw.strip()
        if not line:
            continue
        m = WORKTREE_MARKER_LINE.match(line)
        if m:
            token = _declared_value(m.group("v"))
            if token and _REF_SHAPED.match(token) and not _WORKTREE_PLACEHOLDER.match(token):
                out.append((token, "declared", line[:110]))
        for d in WORKTREE_DIRECTIVE.finditer(line):
            token = (d.group("v") or "").rstrip(".,;:)")
            if token:
                out.append((token, "directive", line[:110]))
    return out


def _hermes_root() -> Path:
    """The FLEET root, not the active profile's: the board is shared across profiles."""
    for var in ("HERMES_KANBAN_HOME", "HERMES_HOME"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return Path(val).expanduser()
    return Path.home() / ".hermes"


def _board_meta(board: Any = None) -> dict:
    """``board.json`` for the active board, read without importing hermes_cli.

    Mirrors ``kanban_db.kanban_home()/boards_root()``: the call's explicit ``board`` arg
    (2026-09-23: the tool takes one, and the kernel reads THAT board's project_id), else
    HERMES_KANBAN_BOARD, else ``<root>/kanban/current``, else ``default``. Absent/unreadable ->
    {} (fail open). No fallback to ANOTHER board's metadata: the wrong board's default_workdir
    is a wrong answer.
    """
    root = _hermes_root()
    slug = str(board or "").strip() or (os.environ.get("HERMES_KANBAN_BOARD") or "").strip()
    if not slug:
        try:
            slug = (root / "kanban" / "current").read_text().strip().splitlines()[0]
        except Exception:                                   # noqa: BLE001
            slug = "default"
    try:
        return json.loads((root / "kanban" / "boards" / (slug or "default") / "board.json")
                          .read_text())
    except Exception:                                       # noqa: BLE001
        return {}


def _project_primary_path(token: Any) -> Optional[str]:
    """The repo a project id/slug points at, from the projects.db stores, or None.

    Read-only sqlite, stdlib only. The store is per-profile, so both the active profile's and
    the fleet root's are tried; everything is exception-guarded (fail open).
    """
    tok = str(token or "").strip()
    if not tok:
        return None
    root = _hermes_root()
    seen = []
    for cand in (Path(os.environ.get("HERMES_HOME") or root) / "projects.db", root / "projects.db"):
        if cand not in seen:
            seen.append(cand)
    for db in seen:
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute(
                    "SELECT primary_path FROM projects WHERE (id = ? OR slug = ?) LIMIT 1",
                    (tok, tok)).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return str(row[0])
        except Exception:                                   # noqa: BLE001
            continue
    return None


def _git(repo, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, timeout=10)
    except Exception:                                       # noqa: BLE001
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for(path: Path) -> Optional[Path]:
    """The PRIMARY repo root that a worktree target belongs to (walk up, like the kernel does)."""
    current = _nearest_existing(path).resolve()
    while True:
        common = _git(current, "rev-parse", "--git-common-dir")
        if common:
            git_dir = Path(common)
            if not git_dir.is_absolute():
                git_dir = (current / git_dir).resolve()
            return git_dir.parent
        if current == current.parent:
            return None
        current = current.parent


def _repo_head_and_refs(repo) -> tuple:
    """``(head_branch, local_branch_names)``. A detached HEAD returns ``(None, set())``."""
    root = str(repo)
    head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if not head or head == "HEAD":
        return None, set()
    out = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads") or ""
    return head, set(name for name in out.splitlines() if name)


def worktree_repo_for(args: dict) -> Optional[str]:
    """The repo a ``worktree`` card will be provisioned in, or None when it cannot be known.

    Resolution mirrors the create path (``kanban_db.create_task``): an explicit
    ``workspace_path`` wins, else the resolved ``project``'s primary path, else the board's
    project / ``default_workdir``. An explicit project we cannot resolve returns None rather
    than falling back to the board's repo — answering about the wrong repo is worse than
    answering nothing, because that is a refusal on a card that was never at fault.
    """
    path = str(args.get("workspace_path") or "").strip()
    if path:
        root = _repo_root_for(Path(path).expanduser())
        return str(root) if root else None
    project = str(args.get("project") or "").strip()
    if project:
        return _project_primary_path(project)
    meta = _board_meta(args.get("board"))
    return _project_primary_path(meta.get("project_id")) or (meta.get("default_workdir") or None)


def worktree_base_conflict(args: Any, resolve_repo=None, repo_refs=None) -> Optional[str]:
    """Refusal reason for a ``worktree`` card that must sit on a branch other than HEAD.

    ``resolve_repo`` / ``repo_refs`` are the injectable seams the tests use; in production they
    are :func:`worktree_repo_for` and :func:`_repo_head_and_refs`.
    """
    if not isinstance(args, dict):
        return None
    if str(args.get("workspace_kind") or "").strip().lower() != "worktree":
        return None                       # scratch and dir cards are untouched by this rule
    cites = branch_citations(args.get("body"))
    if not cites:
        return None                       # cheap first: a body naming no branch never touches git
    repo = (resolve_repo or worktree_repo_for)(args)
    if not repo:
        return None
    head, refs = (repo_refs or _repo_head_and_refs)(repo)
    if not head:
        return None                       # detached HEAD / unreadable repo — fail open
    for token, kind, _line in cites:
        ref = _bare_ref(token)
        if not ref or ref == head:
            continue
        # A DECLARED branch is the author naming the branch by hand, so it is read even when no
        # such ref exists yet — provided it is shaped like a branch. A DIRECTIVE is prose, so it
        # is only read when the token is provably a ref of this repo.
        if kind == "declared" and (ref in refs or _REF_SEPARATOR.search(ref)):
            pass
        elif kind == "directive" and ref in refs:
            pass
        else:
            continue
        return (f"{WORKTREE_REASON_PREFIX}: the body names {ref!r} as the branch this card's "
                f"work sits on, but a `worktree` card is cut from the repo's current HEAD "
                f"({head!r} in {repo})")
    return None


DIR_REASON_PREFIX = "dir workspace"


def dir_workspace_conflict(args: Any, repo_root_for=None, board_reader=None) -> Optional[str]:
    """Refusal reason for a ``dir`` card whose workspace is not a populated git work tree.

    Card t_a8fa2e12 (2026-09-16): a `dir` card minted at an EMPTY directory was spawned anyway,
    the worker hunted for its subject, landed in the deployed `hermes-agent` checkout and
    committed there unreviewed. A `dir` card means "work in this existing tree" — so the tree
    must exist, hold something, and be inside a git repo. An absent ``workspace_path`` is left
    to the kernel's own defaults (fail open); only an EXPLICIT path that cannot carry the work
    is refused.
    """
    if not isinstance(args, dict):
        return None
    if str(args.get("workspace_kind") or "").strip().lower() != "dir":
        return None
    path = str(args.get("workspace_path") or "").strip()
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"{DIR_REASON_PREFIX} {path!r} does not exist (or is not a directory)"
    try:
        if not any(p.iterdir()):
            return f"{DIR_REASON_PREFIX} {path!r} is an EMPTY directory"
    except OSError:
        return None
    root = (repo_root_for or _repo_root_for)(p)
    if root is None:
        return f"{DIR_REASON_PREFIX} {path!r} is not inside a git repository"
    # The LIVE platform checkout is never a build workspace. Workers that edited it in place
    # are how six unapproved kernel commits reached `fleet` on 2026-09-16/17 and how the
    # checkout sat on a test branch for two hours on 2026-09-12. Review/verify cards may read it.
    live = _live_platform_root()
    lane = _lane(str(args.get("title") or ""))
    if live is not None and Path(root).resolve() == live and lane == "build":
        return (f"{DIR_REASON_PREFIX} {path!r} is the LIVE platform checkout and this is a build-lane "
                "card; build in a `worktree` card (or a `dir` card at a worktree from `git worktree list`)")
    if lane == "build":
        other = _concurrent_build_at(p, board_reader)
        if other:
            return (f"{DIR_REASON_PREFIX} {path!r} already carries a build-lane card in flight ({other}); "
                    "two builds committing into one shared tree swallow each other's deliverables "
                    "(card t_0ec7e1d5) — link this card behind it, or give it its own `worktree`")
    return None


def _concurrent_build_at(p: Path, board_reader=None) -> Optional[str]:
    """Id+title of a running/ready build-lane `dir` card at the same path, or None. Fail-open."""
    try:
        rows = (board_reader or _dir_cards_in_flight)(str(p))
    except Exception:  # noqa: BLE001
        return None
    for tid, title in rows or ():
        if _lane(str(title or "")) == "build":
            return f"{tid} {str(title or '')[:60]!r}"
    return None


def _dir_cards_in_flight(path: str):
    """Running/ready `dir` cards at `path` from the shared board — the same store the kernel reads."""
    from hermes_cli.kanban_db_connect import connect_closing  # noqa: PLC0415
    want = {str(Path(path)), str(Path(path).resolve())}
    with connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, title, workspace_path FROM tasks WHERE workspace_kind = 'dir' "
            "AND status IN ('running', 'ready')").fetchall()
    return [(r[0], r[1]) for r in rows if str(r[2] or "") in want]


def _live_platform_root() -> Optional[Path]:
    """`<fleet root>/hermes-agent`. In a worker HERMES_HOME is `<fleet root>/profiles/<p>`, so
    climb out of `profiles/` first — the profile home never holds the checkout."""
    try:
        root = _hermes_root()
        if root.parent.name == "profiles":
            root = root.parent.parent
        p = (root / "hermes-agent").resolve()
        return p if p.is_dir() else None
    except OSError:
        return None


def _dir_message(reason: str) -> str:
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        "A `dir` card tells the worker \"the work lives in this existing tree\". When the tree is "
        "empty, missing or not a git checkout, the worker has nothing to work IN, hunts the "
        "filesystem for its subject, and on 2026-09-16 one landed in the deployed platform checkout "
        "and committed there unreviewed (card t_a8fa2e12).\n\n"
        "Proceed one of three ways:\n"
        "  * the work belongs in an existing checkout -> point `workspace_path` at it (a git "
        "worktree from `git worktree list`, or the project's primary path).\n"
        "  * the deliverable is a fleet script/watchdog/plugin -> use `workspace_kind='scratch'` "
        "and name the destination file in the body; or `worktree` under the platform repo.\n"
        "  * you meant a new worktree -> `workspace_kind='worktree'` and let the dispatcher cut it.\n"
    )


def _worktree_message(reason: str) -> str:
    branch = "that branch"
    m = re.search(r"the body names '([^']+)'", reason)
    if m:
        branch = f"`{m.group(1)}`"
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        "A `worktree` card's base is the primary repo's CURRENT HEAD — nothing in the create "
        "path accepts a base ref (`_ensure_git_worktree()` runs `git worktree add -b <branch> "
        "<target> HEAD`). When the work has to sit on a sibling branch, the worker lands in the "
        "wrong tree: the file it was told to edit is absent, or it edits a branch nobody will "
        "merge — and NOTHING ERRORS. Four such cards were found by hand on 2026-09-16 "
        "(t_75ebec63, t_c827851e, t_331cb549, t_d5cdd9f2); one re-authored a page from scratch "
        "against a premise that was false on its own base.\n\n"
        "Proceed one of three ways:\n"
        f"  * the work must sit on {branch} -> mint this card `workspace_kind='dir'` with\n"
        "    `workspace_path` pointing at an EXISTING worktree that already carries it\n"
        "    (`git worktree list` finds them; confirm the file the card edits is there first).\n"
        "    Then say \"commit and push on that branch\" in the body. Nothing else changes.\n"
        f"  * {branch} does not exist yet and this card is meant to CREATE it -> drop the claim\n"
        "    from the body (a worktree card already gets its own branch), or mint the card that\n"
        "    creates the branch first and point this card at it with a `dir` workspace.\n"
        "  * the work really does belong on HEAD's branch -> delete the line that names\n"
        f"    {branch}. A body that names no branch is untouched by this rule.\n\n"
        "Not in scope: accepting a base ref at create (a kernel change — never the fallback), "
        "or auto-creating worktrees."
    )


def verdict(title: str, assignee: str, body: Any = "", args: Any = None) -> Optional[str]:
    """Pure decision function — unit-tested. Returns a refusal reason, or None."""
    body = _text(body)
    # The kernel rule is about the DELIVERABLE, so it is independent of assignee and runs
    # FIRST — a card minted with no assignee, or an unknown one, must not skip it.
    approved = _core_approved(body)
    if not approved:
        hit = kernel_edit_line(body or "")
        if hit:
            return f"this card instructs a change to upstream's kernel — {hit}"
        # Same deliverable, DECLARED instead of described: the author's own statement that
        # they could not find a sanctioned seam. Paired with the rule above, not replacing
        # it — the verb rule catches what is plainly written, this catches what is declared,
        # so both "patch kanban_db.py" and "my extension point is the kernel" are covered.
        # Both stand down on `core-patch-approved:`.
        off = declared_extension_point(body or "")
        if off:
            return (f"extension-point {off!r} is not one of the five sanctioned seams "
                    f"({' / '.join(EXTENSION_POINTS)}); read only line-initial markers "
                    "or short labelled parenthesis/bracket declarations")

    # The workspace base is a property of the DELIVERABLE too, so it is read here, before the
    # assignee rules and before `assignee-override:` can stand anything down: a cross-lane
    # routing hatch must not also wave through a card whose tree will be the wrong one.
    if isinstance(args, dict):
        base_reason = worktree_base_conflict(args)
        if base_reason:
            return base_reason
        dir_reason = dir_workspace_conflict(args)
        if dir_reason:
            return dir_reason
        # Rule 7 (2026-09-23): the tenant/project/base contract. Also a property of the
        # DELIVERABLE (which repo, which branch namespace), so it sits with rules 5 and 6.
        tenant_reason = tenant_project_conflict(args)
        if tenant_reason:
            return tenant_reason
        land_reason = land_card_conflict(args)
        if land_reason:
            return land_reason

    a = (assignee or "").strip().lower()
    if not a or not (title or "").strip():
        return None                              # nothing to contradict
    if a not in KNOWN:
        # This used to read "unknown names are core's job (it parks them)". Core does park them --
        # EXCEPT on a card created `blocked`, where the create-time check is deliberately skipped
        # ("a blocked card is never dispatched anyway"). True at create time, false at unblock:
        # 2026-09-13, t_6d53f54c was minted blocked/operator_hold with assignee `smith`, nothing
        # complained for two hours, and releasing the hold produced a silent re-block with
        # kind=null. That carve-out is upstream's and not ours to change, so the hole is closed
        # here instead, at the tool path, where initial status is irrelevant.
        if _assignee_is_phantom(assignee):
            return f"assignee {assignee!r} names no Hermes profile, so this card can never dispatch"
        return None
    if OVERRIDE in (body or "").lower():
        return None                              # deliberate cross-lane, declared

    lane = _lane(title)
    marker = _marker_owner(title)

    if marker and marker != a:
        return (f"the title names {marker} as the owner but assignee is '{a}'")
    if lane == "review" and a == "rodge":
        return ("this review-lane card must come from the implementation card's "
                "review_requested handoff, so changes-requested verdicts route back "
                "to the implementer")
    if lane and a in NON_LANE:
        why = {"axel": "axel is WeRoll-business-only and is not a builder",
               "switch": "switch is the switchboard and takes no board work",
               "brain": "brain has no kanban toolset and takes no cards"}[a]
        return (f"this is a {lane}-lane card and {why}")
    if lane == "build" and a in CROSS_LANE_BLOCK:
        return (f"this is a build-lane card but assignee is '{a}', who owns the "
                f"{'review' if a == 'rodge' else 'verify' if a == 'steve-o' else 'design'} lane")
    return None


def _core_message(reason: str) -> str:
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        "Upstream's kanban is the substrate — `hermes_cli/`, `tools/`, `agent/` and "
        "`gateway/` are not ours to patch (decision 2026-09-09). On 2026-09-12 a batch of "
        "board-improvement cards patched the kernel anyway: one merge rewrote "
        "tools/kanban_tools.py by +3891/-1945, silently dropped code another card had added "
        "ninety minutes earlier, and spawned three more cards to repair the damage. All of it "
        "was reverted.\n\n"
        "Re-scope to the first of these that fits:\n"
        "  1. a PLUGIN on an upstream hook — `pre_tool_call` is the fail-closed one, "
        "`kind: backend` so it reaches workers (see plugins/kanban-mint-guard);\n"
        "  2. a `no_agent` WATCHDOG in scripts/ plus a cron entry — zero tokens, reports;\n"
        "  3. a SOUL or SKILL rule, when what you are fixing is a judgement not a mechanism;\n"
        "  4. config.\n\n"
        "If none of them fits, take it to Richie — a core patch is not the fallback. If this "
        f"really is an approved kernel change, put '{CORE_OVERRIDE} <who approved it>' in the body."
    )


def _message(title: str, assignee: str, reason: str) -> str:
    lane = _lane(title)
    expected = LANE_OWNER.get(lane or "", "the right lane owner")
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        f"  title:    {title[:120]}\n"
        f"  assignee: {assignee}\n"
        f"  expected: {expected}\n\n"
        "Mint-time routing is the defect that cost 2026-09-12 three mis-routed cards — "
        "a build card to rodge (57 minutes of heartbeats, no work), a coding card to axel "
        "(timed out at 602s), and an env card to axel again. In all three the card body "
        "said plainly who should own it.\n\n"
        "Re-mint with the correct assignee. If this really is a deliberate cross-lane "
        f"card, put '{OVERRIDE} <reason>' in the body and it will be allowed."
    )


def _review_message(reason: str) -> str:
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        "A standalone review starts outside the review lane, so a changes-requested verdict "
        "cannot be routed back to an implementer. For a single-card review, request it from "
        "the implementation card with `kanban_request_review(reviewer='rodge')`. For a "
        "deliberate branch or wave review, put `assignee-override: <reason>` in the body."
    )


def _ladder_message(reason: str) -> str:
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        "The card DECLARES an extension point, and a declaration off the ladder is the author\n"
        "saying they could not find a sanctioned seam. That is exactly the case that goes to\n"
        "Richie instead of onto the board — so it does not get a lane.\n\n"
        "Every improvement is tried in this order:\n"
        "  1. plugin    — hermes-agent/plugins/<name>, `kind: backend` so it loads in every\n"
        "                 profile AND every kanban worker; the hooks are the seam. `pre_tool_call`\n"
        "                 is the fail-closed one. Zero merge surface if it imports nothing from\n"
        "                 hermes_cli (see plugins/kanban-mint-guard, plugins/kanban-project-link-guard).\n"
        "  2. watchdog  — a `no_agent` cron script in scripts/fleet-watchdogs/: zero tokens,\n"
        "                 silent unless something is wrong (see core-patch-watch.py).\n"
        "  3. skill     — a SKILL.md on the assignee's profile, attached per card via `skills:`;\n"
        "                 loaded only for the jobs that need it.\n"
        "  4. soul      — a dated block. Steering, not enforcement. Net-zero by default: name the\n"
        "                 line it removes.\n"
        "  5. config    — a declared key, on EVERY profile (the scoping law).\n\n"
        "A CORE PATCH IS NEVER THE FALLBACK. It is invisible merge surface and the cost lands on\n"
        "whoever takes the next upstream catch-up.\n\n"
        "Proceed one of three ways:\n"
        "  * re-scope the card to the first rung that fits and declare it: `extension-point: <seam>`;\n"
        "  * if none of the five fits, take it to Richie and say so in the card — with the reason,\n"
        "    not a kernel lane. If Richie approves a core change, add `core-patch-approved: <who>`\n"
        "    and the guard stands down;\n"
        "  * if that `extension-point:` line was only quoted from the ladder's own wording — or it\n"
        "    was a placeholder like `<value>` — drop the marker (or make it a ladder value). A body\n"
        "    with no marker is untouched by this rule."
    )


def _message_for(title: str, assignee: str, reason: str) -> str:
    """Pick the refusal text by the CLASS of reason, never by a substring of the value.

    `declared_extension_point` returns the raw token, so an off-ladder value spells its own
    word — `config/kernel` contains "kernel" and would otherwise be answered with the core
    message, which is not what it said. The prefix is the discriminator; the extension rule's
    reason is built to start with it.
    """
    if reason.startswith(WORKTREE_REASON_PREFIX):
        return _worktree_message(reason)
    if reason.startswith(DIR_REASON_PREFIX):
        return _dir_message(reason)
    if reason.startswith(TENANT_REASON_PREFIX):
        return _tenant_message(reason)
    if reason.startswith(LAND_REASON_PREFIX):
        return _land_message(reason)
    if reason.startswith("extension-point"):
        return _ladder_message(reason)
    if reason.startswith("this review-lane card"):
        return _review_message(reason)
    if "kernel" in reason:
        return _core_message(reason)
    return _message(title, assignee, reason)



# ---------------------------------------------------------------------------
# Rule 6 (2026-09-18, freeze-20260918, Richie): PLATFORM FINDINGS GO TO A LIST.
#
# Measured over 22 Aug - 18 Sep: 491 of 921 costed cards (53%) and $95.65 of $226.93
# (42%) were the platform working on itself, and at peak 113 platform cards were minted
# in 24 hours, 68 of them by overwatch. Each became a branch, a review round and a guard.
# A finding is cheap; a card is not. So the finding is RECORDED and the card is refused.
#
# Escape hatch: `platform-approved:` in the body (Richie picks from the list weekly).
# Fail-open like every other rule here - any error and the card is created.
PLATFORM_FINDINGS = "PLATFORM-FINDINGS.md"
PLATFORM_OVERRIDE = "platform-approved:"
PLATFORM_TITLE = re.compile(r"^\s*\[\s*(platform|held)\b", re.I)
# A business tag always wins: these are product cards even if they mention the platform.
BUSINESS_TAG = re.compile(r"^\s*\[\s*(backupbrain|release|rova|weroll|mediaworks)\b", re.I)


def _record_platform_finding(title: str, assignee: str, body: str) -> str:
    """Append the finding to the list and return the path written."""
    p = _hermes_root() / PLATFORM_FINDINGS
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    text = str(body or "").strip()
    if len(text) > 1500:
        text = text[:1500] + "\n... (truncated)"
    entry = (f"\n## {stamp} - {title.strip()}\n\n"
             f"- proposed assignee: `{assignee or '(none)'}`\n"
             f"- status: unreviewed\n\n{text}\n")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(entry)
    return str(p)


def platform_finding(title: str, assignee: str, body: str) -> Optional[str]:
    """Return a refusal reason when this card is platform self-work."""
    try:
        t = str(title or "")
        b = str(body or "")
        if PLATFORM_OVERRIDE in b.lower():
            return None
        if BUSINESS_TAG.match(t):
            return None
        if not PLATFORM_TITLE.match(t):
            return None
        return "platform self-work - recorded as a finding instead of a card"
    except Exception:  # noqa: BLE001
        return None


def _platform_message(path: str) -> str:
    return (
        "Refusing to mint this card: it is PLATFORM SELF-WORK.\n\n"
        "The finding has been recorded instead, at:\n"
        f"  {path}\n\n"
        "From 2026-09-18 the board builds the product; it does not build itself. Platform\n"
        "findings accumulate in that file and Richie picks what gets done, weekly. This is a\n"
        "measured decision, not a preference: over the month to 18 Sep, 53% of cards and 42%\n"
        "of spend went to self-work while median cycle time tripled and first-pass review fell\n"
        "to its worst recorded level.\n\n"
        "Nothing is lost. Write the finding well - what breaks, the evidence, and the smallest\n"
        "seam that would fix it - because that file is what gets read.\n\n"
        "If Richie has already approved this specific piece of platform work, put\n"
        "`platform-approved: <reason>` in the body and the guard stands down."
    )




# ---------------------------------------------------------------------------
# Rule 7 (2026-09-23, p1-20260923 / P2-mint-contract): THE MINT-TIME TENANT /
# PROJECT / BASE CONTRACT.
#
# THE DEFECT (kernel `hermes_cli/kanban_db.py`, read only): an explicit `tenant` never
# binds the project. `create_task` fills `project_id` from the BOARD's default first
# (:1357-1361) and consults the tenant map only when `project_obj is None and
# workspace_kind == "scratch"` (:1402-1404). On the default board — whose board.json
# `project_id` is `p_3d4a6fe1` (BackupBrain) — a `[WeRoll]` card minted with
# `tenant="weroll-app"` and no `project` therefore binds BackupBrain, and its worktree is
# cut on a `backupbrain/t_…` branch (t_32e1fb81, 2026-09-22 21:35, the third such card).
# The `kanban-mint` skill already says "always pass both tenant AND project"; an
# instruction is not enforcement (see hold-marker-guard for the same lesson).
#
# The kernel is frozen, so the contract is enforced HERE, on the args the kernel will
# see, in three parts — each fail-open on any exception, like every rule above:
#   7a  a worktree card's PROJECT must belong to the same tenant as its REPO. The repo
#       tenant is resolved from the explicit `tenant` arg, else the title's business tag,
#       else an explicit `workspace_path`, else a `worktree:` / `repo:` / `workspace:` line
#       in the body, else the board's `default_workdir`. The project the kernel WOULD bind
#       is the explicit `project` arg, else the board's `project_id`. When the two name
#       different tenants the mint is REFUSED and the message says exactly what to pass.
#   7b  a BUILD-lane worktree card carries `branch-target: <trunk>` (the kanban-mint
#       skill's spelling). Absent, it is INJECTED — `{"action": "modify"}`, the same
#       mechanism hold-marker-guard uses — never refused. The trunk is the tenant's
#       `trunk` key when present, else `main`.
#   7c  a LAND card (`[Release] Land PR #<n>`) must open with `operator-hold: manual` or
#       carry a `hold: <condition>` line, and only one land card per tenant may be
#       `running` at a time. Escape hatch: `land-serialisation: waived <reason>`.
#
# Overlap with kanban-project-link-guard: none — that guard refuses only an EXPLICIT
# `project` that resolves nowhere; this rule reads a project that DOES resolve and asks
# whether it is the right one. Tenants that are two spellings of one repo (`backupbrain`
# and `backupbrain-legacy` share `ci_gate.repo`) are the same tenant to this rule, so the
# default board's legacy `default_workdir` cannot refuse a BackupBrain card.
TENANT_REASON_PREFIX = "tenant/project"
LAND_REASON_PREFIX = "land card"
BRANCH_TARGET_KEY = "branch-target"
BRANCH_TARGET_LINE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)?(?:\*\*|__|`)?[ \t]*branch[-_ ]target[ \t]*(?:\*\*|__|`)?"
    r"[ \t]*[:=][ \t]*(?P<v>\S+)", re.I | re.M)
# `worktree: ~/Projects/weroll-app` / `repo: …` / `workspace: …` — the body's own statement of
# the repo (the kanban-mint skill's "workspace:" line is the third spelling).
REPO_REF_LINE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)?(?:\*\*|__|`)?[ \t]*(?:worktree|repo|workspace)[ \t]*(?:\*\*|__|`)?"
    r"[ \t]*:[ \t]*`?(?P<v>~?/[^`\s]+)`?", re.I | re.M)
LAND_TITLE = re.compile(r"^\s*\[\s*release\s*\]\s*land\s+pr\s*#\s*(?P<n>\d+)", re.I)
# The hold-marker-guard / release-operator-hold-watch line, restricted to `manual`.
_HOLD_MANUAL = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+|>[ \t]*|#{1,6}[ \t]+)*"
    r"(?:\*\*|__|`)?[ \t]*operator-hold[ \t]*:[ \t]*(?:\*\*|__|`)?[ \t]*manual\b", re.I)
# hold-condition-watch's `hold: <type> [value]` line (its own parser: `^hold:\s*(\S+)`).
_HOLD_CONDITION = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?`?hold`?[ \t]*:[ \t]*(?P<v>\S+)", re.I | re.M)
_LAND_WAIVER = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?`?land-seriali[sz]ation`?[ \t]*:[ \t]*waived\b(?P<why>[^\n]*)",
                          re.I | re.M)
_LAND_STATUSES = ("running",)
_TITLE_TENANT_TAG = re.compile(r"^\s*\[\s*(?P<t>[a-z][a-z0-9 ._-]{1,20})\s*\]", re.I)


def _fleet_root() -> Path:
    """`~/.hermes` for the FLEET, never a profile dir. In a worker HERMES_HOME is
    `<fleet>/profiles/<p>`; the tenant map lives one level up (kernel `_fleet_home`)."""
    root = _hermes_root()
    try:
        if root.parent.name == "profiles":
            root = root.parent.parent
    except Exception:  # noqa: BLE001
        pass
    return root


def _tenants() -> dict:
    """`kanban-tenants.json` as a dict, or {} on ANY problem (a broken file stands rule 7 down).
    `HERMES_KANBAN_TENANTS` overrides the path, exactly as it does for the kernel."""
    try:
        override = (os.environ.get("HERMES_KANBAN_TENANTS") or "").strip()
        p = Path(override).expanduser() if override else _fleet_root() / "kanban-tenants.json"
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("primary_path")}
    except Exception:  # noqa: BLE001
        return {}


def _norm_path(p: Any) -> Optional[str]:
    s = str(p or "").strip().strip("`'\"")
    if not s:
        return None
    try:
        path = Path(s).expanduser()
        try:
            path = path.resolve()
        except Exception:  # noqa: BLE001
            pass
        return str(path).rstrip("/")
    except Exception:  # noqa: BLE001
        return None


def _tenant_for_path(path: Any, tenants: dict) -> Optional[str]:
    """The tenant KEY whose `primary_path` is `path` or contains it (longest match), or None."""
    target = _norm_path(path)
    if not target:
        return None
    best, best_len = None, -1
    for key, ent in tenants.items():
        prim = _norm_path(ent.get("primary_path"))
        if not prim:
            continue
        if target == prim or target.startswith(prim + "/"):
            if len(prim) > best_len:
                best, best_len = key, len(prim)
    return best


def _tenant_for_name(name: Any, tenants: dict) -> Optional[str]:
    """Tenant KEY for a tenant key / slug / display name / project id, case-insensitive."""
    tok = str(name or "").strip().strip("`'\"").lower()
    if not tok:
        return None
    for key, ent in tenants.items():
        if tok in {str(key).lower(), str(ent.get("slug") or "").lower(),
                   str(ent.get("name") or "").lower(), str(ent.get("id") or "").lower()}:
            return key
    return None


def _same_tenant(a: str, b: str, tenants: dict) -> bool:
    """Two tenant keys are one tenant when equal or when they gate the same GitHub repo
    (`backupbrain` and `backupbrain-legacy` are two paths to one project)."""
    if a == b:
        return True
    ra = str(((tenants.get(a) or {}).get("ci_gate") or {}).get("repo") or "").lower()
    rb = str(((tenants.get(b) or {}).get("ci_gate") or {}).get("repo") or "").lower()
    return bool(ra) and ra == rb


def _explicit_project(args: dict) -> Optional[str]:
    """The caller's explicit `project` (key PRESENT, like kanban-project-link-guard reads it);
    "" means "no project" and returns ""; absent returns None."""
    raw = args["project"] if "project" in args else args.get("project_id")
    if raw is None:
        return None
    return str(raw).strip()


def repo_tenant_for(args: dict, tenants: dict, meta: Optional[dict] = None) -> Optional[str]:
    """The tenant whose repo this card's work lands in, by the sources listed on rule 7a."""
    key = _tenant_for_name(args.get("tenant"), tenants)
    if key:
        return key
    m = _TITLE_TENANT_TAG.match(str(args.get("title") or ""))
    if m:
        key = _tenant_for_name(m.group("t"), tenants)
        if key:
            return key
    key = _tenant_for_path(args.get("workspace_path"), tenants)
    if key:
        return key
    m = REPO_REF_LINE.search(_text(args.get("body")))
    if m:
        key = _tenant_for_path(m.group("v"), tenants)
        if key:
            return key
    meta = meta if meta is not None else _board_meta(args.get("board"))
    return _tenant_for_path(meta.get("default_workdir"), tenants)


def bound_project_for(args: dict, meta: Optional[dict] = None) -> tuple:
    """``(project_id, source)`` the kernel would bind: explicit `project`, else the board's
    `project_id`. ``(None, …)`` when no project would bind (explicit "" or a project-less board)."""
    explicit = _explicit_project(args)
    if explicit is not None:
        return (explicit or None), "explicit"
    meta = meta if meta is not None else _board_meta(args.get("board"))
    pid = str(meta.get("project_id") or "").strip() or None
    return pid, f"board `{meta.get('slug') or os.environ.get('HERMES_KANBAN_BOARD') or 'default'}` default"


def in_worktree_scope(args: dict) -> bool:
    """Rule 5's test (`workspace_kind == 'worktree'`) widened by the shapes the kernel UPGRADES
    to a worktree: no `workspace_kind` at all plus a project that will bind, an explicit
    `tenant`, or a `worktree:`/`repo:`/`workspace:` line. An explicit `scratch`/`dir` is out."""
    kind = str(args.get("workspace_kind") or "").strip().lower()
    if kind == "worktree":
        return True
    if kind:
        return False
    if str(args.get("tenant") or "").strip():
        return True
    if REPO_REF_LINE.search(_text(args.get("body"))):
        return True
    return _explicit_project(args) not in (None, "")


def _board_slug_for_project(pid: str) -> Optional[str]:
    """A board whose board.json binds `pid`, for the refusal's `--board` hint. None if none."""
    try:
        root = _hermes_root() / "kanban" / "boards"
        for child in sorted(root.iterdir()):
            try:
                meta = json.loads((child / "board.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(meta, dict) and str(meta.get("project_id") or "") == pid and not meta.get("archived"):
                return child.name
    except Exception:  # noqa: BLE001
        pass
    return None


def tenant_project_conflict(args: Any, tenants: Optional[dict] = None,
                            board_meta: Optional[dict] = None) -> Optional[str]:
    """Refusal reason for rule 7a, or None. `tenants` / `board_meta` are the tests' seams."""
    try:
        if not isinstance(args, dict) or not in_worktree_scope(args):
            return None
        tenants = tenants if tenants is not None else _tenants()
        if not tenants:
            return None                       # no map, broken map -> nothing to check against
        meta = board_meta if board_meta is not None else _board_meta(args.get("board"))
        repo_key = repo_tenant_for(args, tenants, meta)
        if not repo_key:
            return None                       # the repo is not a tenant's -> not our contract
        pid, source = bound_project_for(args, meta)
        if not pid:
            return None                       # no project binds -> a scratch card, rule 5's world
        proj_key = _tenant_for_name(pid, tenants)
        if not proj_key:
            return None                       # a projects.db-only project: cannot judge, fail open
        if _same_tenant(repo_key, proj_key, tenants):
            return None
        want = tenants[repo_key]
        got = tenants[proj_key]
        return (f"{TENANT_REASON_PREFIX}: this card's work lands in {want['primary_path']} "
                f"(tenant `{repo_key}`), but the project the kernel would bind is `{pid}` "
                f"({got.get('name') or proj_key}, from the {source}) — its worktree would be cut in "
                f"{got['primary_path']} on a `{got.get('slug') or proj_key}/t_…` branch")
    except Exception:  # noqa: BLE001
        return None


def _tenant_message(reason: str) -> str:
    tenants = _tenants()
    m = re.search(r"tenant `([^`]+)`", reason)
    key = m.group(1) if m else None
    ent = tenants.get(key or "", {}) if tenants else {}
    pid = ent.get("id") or f"<{key or 'tenant'}'s project id>"
    board = _board_slug_for_project(str(pid)) if ent else None
    board_hint = (f"`board=\"{board}\"` (CLI: `--board {board}`)" if board
                  else f"a board whose board.json `project_id` is `{pid}` (none exists yet — "
                       f"`hermes kanban boards create <slug> --default-workdir {ent.get('primary_path', '<repo>')}` "
                       f"and set `project_id` in its board.json)")
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        "An explicit `tenant` never binds the project: `create_task` takes the BOARD's default\n"
        "`project_id` first and consults the tenant map only for a project-less scratch card. So a\n"
        "card for one tenant minted on another tenant's board gets the wrong repo AND a branch\n"
        "namespaced to the wrong project — t_32e1fb81 (2026-09-22) was the third such card: a\n"
        "`[WeRoll]` card whose branch was `backupbrain/t_32e1fb81-…`. The kanban-mint skill\n"
        "already says to pass both; this rule makes it so.\n\n"
        "Proceed one of two ways:\n"
        f"  * pass `project=\"{pid}\"` together with `tenant=\"{key or '<tenant>'}\"` on this call;\n"
        f"  * or mint it on that tenant's own board: {board_hint}.\n\n"
        "If the project really is the right one, name the repo the card should use: an explicit\n"
        "`workspace_path` under that project's `primary_path`, or drop the `tenant`/`worktree:`\n"
        "claim that names the other repo."
    )


def branch_target_injection(args: Any, tenants: Optional[dict] = None,
                            board_meta: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Rule 7b: the args to ADD (`{"body": ...}`) so a build-lane worktree card carries
    `branch-target: <trunk>`, or None when it already does / is out of scope. Never raises."""
    try:
        if not isinstance(args, dict) or not in_worktree_scope(args):
            return None
        if _lane(str(args.get("title") or "")) != "build":
            return None
        body = _text(args.get("body"))
        if BRANCH_TARGET_LINE.search(body):
            return None
        tenants = tenants if tenants is not None else _tenants()
        trunk = "main"
        if tenants:
            meta = board_meta if board_meta is not None else _board_meta(args.get("board"))
            key = repo_tenant_for(args, tenants, meta)
            if not key:
                pid, _src = bound_project_for(args, meta)
                key = _tenant_for_name(pid, tenants) if pid else None
            if key:
                trunk = str((tenants.get(key) or {}).get("trunk") or "main").strip() or "main"
        line = f"{BRANCH_TARGET_KEY}: {trunk}"
        if not body.strip():
            return {"body": line + "\n"}
        return {"body": body.rstrip("\n") + "\n\n" + line + "\n"}
    except Exception:  # noqa: BLE001
        return None


def _board_db_path(board: Any = None) -> Optional[Path]:
    """The board's kanban.db, resolved the way `kanban_db.kanban_db_path` does, without
    importing it: HERMES_KANBAN_DB pins it; `default` -> `<root>/kanban.db`; else the board dir."""
    pinned = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    if pinned:
        return Path(pinned).expanduser()
    root = _hermes_root()
    slug = str(board or "").strip() or (os.environ.get("HERMES_KANBAN_BOARD") or "").strip()
    if not slug:
        try:
            slug = (root / "kanban" / "current").read_text().strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            slug = "default"
    if slug in ("", "default"):
        return root / "kanban.db"
    return root / "kanban" / "boards" / slug / "kanban.db"


def _land_cards_running(tenant: str, board: Any = None):
    """`[(id, title)]` of `running` land cards for `tenant` on the board — stdlib sqlite,
    read-only URI, bounded. Raises on failure; the caller fails open."""
    db = _board_db_path(board)
    if not db or not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        marks = ",".join("?" * len(_LAND_STATUSES))
        rows = conn.execute(
            f"SELECT id, title FROM tasks WHERE status IN ({marks}) AND tenant = ? "
            "AND title LIKE '[Release] Land PR%' LIMIT 20", (*_LAND_STATUSES, tenant)).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rows if LAND_TITLE.match(str(r[1] or ""))]


def land_card_conflict(args: Any, tenants: Optional[dict] = None,
                       board_reader=None) -> Optional[str]:
    """Refusal reason for rule 7c, or None. `board_reader(tenant, board)` is the tests' seam."""
    try:
        if not isinstance(args, dict):
            return None
        title = str(args.get("title") or "")
        m = LAND_TITLE.match(title)
        if not m:
            return None
        body = _text(args.get("body"))
        lines = [ln for ln in body.splitlines() if ln.strip()]
        first_ok = bool(lines) and bool(_HOLD_MANUAL.match(lines[0]))
        if not first_ok and not _HOLD_CONDITION.search(body):
            return (f"{LAND_REASON_PREFIX}: `[Release] Land PR #{m.group('n')}` carries neither "
                    "`operator-hold: manual` as its first line nor a `hold: <condition>` line")
        if _LAND_WAIVER.search(body):
            return None
        tenants = tenants if tenants is not None else _tenants()
        tenant = str(args.get("tenant") or "").strip()
        if not tenant and tenants:
            tenant = repo_tenant_for(args, tenants) or ""
        if not tenant:
            tenant = (os.environ.get("HERMES_TENANT") or "").strip()
        if not tenant:
            return None                       # no tenant to serialise on -> fail open
        try:
            running = (board_reader or _land_cards_running)(tenant, args.get("board"))
        except Exception:  # noqa: BLE001
            return None
        if running:
            tid, other = running[0]
            return (f"{LAND_REASON_PREFIX}: another land card for tenant `{tenant}` is already running "
                    f"({tid} {str(other)[:70]!r})")
        return None
    except Exception:  # noqa: BLE001
        return None


def _land_message(reason: str) -> str:
    if "already running" in reason:
        return (
            f"Refusing to mint this card: {reason}.\n\n"
            "Land cards for one tenant are SERIALISED: two merges to the same trunk in flight race\n"
            "each other's head-identity check and CI tip, and the second lands on a base its review\n"
            "never saw. Mint this one held (`operator-hold: manual` first line, or\n"
            "`hold: wait-pr-merged <n>` naming the running one's PR) so it dispatches after that\n"
            "card is done — or wait for it.\n\n"
            "If both really must run together, put `land-serialisation: waived <reason>` in the body\n"
            "and this rule stands down."
        )
    return (
        f"Refusing to mint this card: {reason}.\n\n"
        "A land card is a human gate. release-operator-hold-watch keeps a hold ONLY when the body\n"
        "declares `operator-hold: manual`; hold-condition-watch auto-releases on a `hold: …` line.\n"
        "A land card with neither is released and dispatched within a minute of minting, which is\n"
        "the 'merged before Richie saw it' defect.\n\n"
        "Re-mint with the body starting\n"
        "    operator-hold: manual\n"
        "or with a machine-readable condition line, one of\n"
        "    hold: wait-pr-merged <number> | hold: wait-main-green | hold: wait-date <ISO> | hold: manual\n"
        "(passing `hold=true` alone appends the marker at the END of the body; the watcher wants it first)."
    )


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, Any]]:
    try:
        if payload.get("tool_name") != "kanban_create":
            return None
        args = payload.get("args") or {}
        title = str(args.get("title") or "")
        assignee = str(args.get("assignee") or "")
        body = str(args.get("body") or "")
        pf = platform_finding(title, assignee, body)
        if pf:
            try:
                path = _record_platform_finding(title, assignee, body)
            except Exception:  # noqa: BLE001 -- never lose a card to a failed write
                logger.exception("kanban-mint-guard: could not record platform finding, allowing")
                return None
            logger.warning("kanban-mint-guard: platform card refused, finding recorded (title=%r)", title[:80])
            return {"action": "block", "message": _platform_message(path)}
        reason = verdict(title, assignee, body, args=args)
        if not reason:
            # Rule 7b: nothing refuses, so a build-lane worktree card without a
            # `branch-target:` line gets one. ``{"action": "modify", "args": {...}}``
            # shallow-merges into the call's args (hermes_cli/plugins.py), exactly as
            # hold-marker-guard appends `operator-hold: manual`.
            add = branch_target_injection(args)
            if add:
                logger.info("kanban-mint-guard: injecting %r into %r", add["body"].splitlines()[-1], title[:80])
                return {"action": "modify", "args": add}
            return None
        logger.warning("kanban-mint-guard: refusing kanban_create — %s (title=%r assignee=%r)",
                       reason, title[:80], assignee)
        return {"action": "block", "message": _message_for(title, assignee, reason)}
    except Exception:  # noqa: BLE001
        # Never stop a board from minting because this guard had a bad day.
        logger.exception("kanban-mint-guard: unexpected error, allowing")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
