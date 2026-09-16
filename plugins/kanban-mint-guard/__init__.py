"""Mint-time assignee routing guard, on upstream's ``pre_tool_call`` hook.

See plugin.yaml for why this is a plugin and not the reverted core patch.

Contract (hermes_cli/plugins.py):
    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

Imports NOTHING from hermes_cli. It decides on the tool arguments alone, so it has
no merge surface at all and cannot be broken by upstream moving a symbol.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

__all__ = ["register", "on_pre_tool_call", "verdict", "declared_extension_point"]

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
#   (a) DECLARED at the start of a line (bullet / heading / quote / bold / backticks allowed);
#   (b) wrapped in INLINE CODE anywhere in a line — `extension-point: config/kernel`.
# (b) is not a widening of the prose rule: formatting the marker as code is the author saying
# "this is the marker", which is why t_331cb549's real body reads
#   "Held: ... work. `extension-point: config/kernel` — `hermes_cli/kanban_db.py`, not code."
# A BARE mention of the phrase inside a sentence (no backticks, not line-initial) is prose
# ABOUT the marker and is deliberately not read — that is the false-negative this rule owns,
# and core-patch-watch is the backstop, exactly as it is for the kernel rule.
#
# The residual false positive, accepted and pinned in the test file: a card that QUOTES an
# off-ladder example in marker form ("a card saying `extension-point: config/kernel` is
# refused") is refused itself. The cost is one refusal message to an author who is present
# and can drop the example; the cost of NOT reading form (b) is the card this rule exists for.
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
EXTENSION_MARKER_CODE = re.compile(r"`[ \t]*extension[-_ ]point[ \t]*[:=][ \t]*(?P<v>[^`\n]+?)[ \t]*`",
                                   re.I)
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
    hits = [(m.start(), m.group("v")) for pat in (EXTENSION_MARKER_LINE, EXTENSION_MARKER_CODE)
            for m in pat.finditer(text)]
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


def verdict(title: str, assignee: str, body: Any = "") -> Optional[str]:
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
                    f"({' / '.join(EXTENSION_POINTS)})")

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
    if reason.startswith("extension-point"):
        return _ladder_message(reason)
    if reason.startswith("this review-lane card"):
        return _review_message(reason)
    if "kernel" in reason:
        return _core_message(reason)
    return _message(title, assignee, reason)


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    try:
        if payload.get("tool_name") != "kanban_create":
            return None
        args = payload.get("args") or {}
        title = str(args.get("title") or "")
        assignee = str(args.get("assignee") or "")
        body = str(args.get("body") or "")
        reason = verdict(title, assignee, body)
        if not reason:
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
