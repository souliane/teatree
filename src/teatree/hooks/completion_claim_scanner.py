r"""Completion-claim gate detector (#2665).

The agent emits a completeness assertion — "done", "no blockers anywhere",
"ready to merge", "everything is here" — from the artifacts it produced
reviewing clean, NOT from every spec-defined deliverable verified on the actual
merge target. The representative failure: "no blockers anywhere" claimed on a
multi-deliverable ticket while the crucial deliverable was on the wrong surface
and its fix was stranded off the merge target, surfaced only later under direct
review. A false completion claim that propagates downstream is a
highest-severity reliability gap. This recurs despite the prose
verification-before-completion rule, so it must become a GATE.

This module is the pure detector the Stop handler
``handle_completion_claim_gate`` in ``hooks/scripts/hook_router.py`` uses. It
fires a BLOCK only on a HIGH-confidence MULTI-DELIVERABLE completion claim that
lacks an on-target deliverable->evidence map. The closure-reverify sibling
(#1448) is WARN-only and deliberately excludes "done"; this gate is the harder
enforcement the issue escalates to — it BLOCKS, with the same never-lockout
escapes as the structured-question gate (kill-switch + per-call token, owned by
the handler).

The detector is tuned HARD for precision over recall, mirroring the
closure-reverify prime directive: a false fire would wedge a legitimate
single-deliverable "done", so when in doubt it stays SILENT (returns "no fire").
The two load-bearing no-fire guards. First, a single-deliverable claim (no
second deliverable enumerated) never fires — the gate is scoped to
multi-deliverable tickets, exactly the issue's scope. Second, a claim
accompanied by a COMPLETE deliverable->on-target-evidence map clears: every
enumerated deliverable carries on-target evidence (merged to target / verified
on the correct surface / passing E2E), the authoritative spec was read, and the
crucial deliverable is explicitly verified on its surface.

An honest "NOT done: <X> stranded/wrong-surface/missing" is the desired
behaviour and must NEVER fire — it is the refusal the gate wants, not a claim.

Fail-safe-to-silent everywhere: empty/odd input yields ``None`` (no fire). The
handler wraps the whole thing in a crash-proof ``try``.
"""

import re
from typing import Final, NamedTuple

# Bare "done" is matched here (the closure-reverify sibling excludes it) because
# the multi-deliverable guard, not the verb breadth, carries this gate's precision.
_COMPLETENESS_CLAIM_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"no blockers(?:\s+anywhere)?|"
    r"everything(?:'s| is| looks)?\s+(?:here|done|complete|good|in place|ready|green)|"
    r"all\s+(?:deliverables?|items?|work|tasks?|of (?:it|them))\s+(?:are\s+|is\s+)?"
    r"(?:done|complete|merged|landed|shipped|in place)|"
    r"ready (?:to|for) (?:merge|review|ship)|"
    r"ready to go|"
    r"good to (?:merge|go|ship)|"
    r"(?:work|ticket|task|feature) is (?:now )?(?:done|complete|finished|shipped)|"
    r"(?:i'?m |we'?re )?done(?: here)?|"
    r"(?:it'?s |this is )?(?:all )?(?:done|complete|finished|shipped)|"
    r"clear to merge"
    r")\b",
    re.IGNORECASE,
)

# An honest refusal overrides any completeness verb in the same turn: a turn
# carrying "NOT done: X is stranded" is the desired behaviour, not a claim.
_NOT_DONE_REFUSAL_RE: Final[re.Pattern[str]] = re.compile(
    r"\bnot\s+done\b|\bnot\s+(?:yet\s+)?(?:complete|finished|merged|ready)\b|"
    r"\bstranded\b|\bwrong surface\b|\boff[- ]target\b|\bnot on (?:the )?(?:merge )?target\b",
    re.IGNORECASE,
)

# One enumerated deliverable line; the count decides "multi-deliverable" and each
# body is inspected for on-target evidence.
_DELIVERABLE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*+]|\d+[.)]|[A-Za-z][.)])\s+(?P<body>\S.*)$",
    re.MULTILINE,
)

# On-target evidence anchors: proof a deliverable landed on the actual merge
# target. "an MR exists" is explicitly NOT evidence and must NOT match here.
_ON_TARGET_EVIDENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"\bmerged (?:to|into|onto) (?:the )?(?:merge )?target\b|"
    r"\bmerged to (?:main|master|develop|the default branch)\b|"
    r"\bon (?:the )?(?:merge )?target\b|"
    r"\bon (?:main|master|develop|the default branch)\b|"
    r"\bverified on (?:the |its )?(?:correct |config |configuration |intended |right )*surface\b|"
    r"\bpassing e2e\b|\be2e (?:passes|passing|green)\b|"
    r"\bverified on (?:the )?deployed\b|"
    r"\bevidence (?:posted|on target)\b",
    re.IGNORECASE,
)

# The forbidden artifact-existence proxy: a line whose only signal is an MR/PR
# existing is NOT on-target evidence.
_ARTIFACT_EXISTS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:mr|pr|merge request|pull request|branch)\s+"
    r"(?:#?!?\d+\s+)?(?:exists|opened|created|raised|up|is open|drafted|ready)\b|"
    r"\b(?:opened|created|raised|drafted)\s+(?:an?\s+)?(?:mr|pr|merge request|pull request)\b",
    re.IGNORECASE,
)

# An explicit "read the spec / spec comments" statement clears the spec-read leg:
# the real failure emitted the claim before the spec source was ever read.
_SPEC_READ_RE: Final[re.Pattern[str]] = re.compile(
    r"\bread (?:the )?(?:authoritative )?spec\b|"
    r"\bspec(?:'s)? comments?\b|"
    r"\bread (?:the )?(?:ticket|issue) (?:and its )?comments?\b|"
    r"\bspec (?:was )?read\b|"
    r"\benumerated (?:every|all|each) (?:spec )?deliverable\b",
    re.IGNORECASE,
)

# The crucial deliverable verified on its correct surface — the one that degraded
# to the wrong surface in the incident; this verification must be explicit.
_CRUCIAL_SURFACE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:crucial|key|authoring|main|primary|critical) deliverable\b[^.\n]*"
    r"\b(?:verified|confirmed|landed|present|registered)\b[^.\n]*\bsurface\b|"
    r"\bverified (?:the )?(?:crucial|key|authoring|main|primary|critical) deliverable\b|"
    r"\bauthoring surface\b[^.\n]*\b(?:correct|verified|confirmed|intended)\b|"
    r"\b(?:correct|intended|config|configuration) surface\b[^.\n]*\b(?:verified|confirmed)\b",
    re.IGNORECASE,
)

# An ARCHITECTURE / PLANNING / RECOMMENDATION frame: the turn is laying out
# options, patterns, or decisions to choose among — NOT claiming delivered work
# is done. The gate targets "all the deliverables are done" on a real
# multi-deliverable TICKET; a recommendation that merely enumerates options must
# never read as a completion claim (#2665 false positive: an architecture-
# recommendation turn enumerating 10 decision items was counted as "10
# deliverables" and demanded an evidence map though NOTHING was claimed done).
_RECOMMENDATION_FRAME_RE: Final[re.Pattern[str]] = re.compile(
    r"\brecommendation\b|\bi(?:'d| would)?\s+(?:recommend|suggest|propose)\b|"
    r"\bdraft proposal\b|\bproposal\b|"
    r"\barchitecture (?:recommendation|proposal|options?|decision)\b|"
    r"\b(?:the )?options?\s+(?:are|below|to (?:consider|choose|weigh))\b|"
    r"\boptions? and trade[- ]?offs?\b|"
    r"\bdecision items?\b|\btrade[- ]?offs?\b|\bpros and cons\b|"
    r"\bopen questions?\b|\bwe could\b|\bwe should consider\b|"
    r"\blaying out the options\b",
    re.IGNORECASE,
)

# An enumerated line that is an OPTION / PATTERN / DECISION / QUESTION rather than
# a unit of delivered work. A recommendation turn is DOMINATED by these; a real
# multi-deliverable done-claim's lines are units of work ("Backend change: MR
# opened"), not "Option A" / "Decision item" / "Trade-off".
_RECOMMENDATION_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:"
    r"option\b|pattern\s+\w|approach\b|alternative\b|"
    r"decision(?:\s+item)?\b|trade[- ]?off\b|pros?\b|cons?\b|"
    r"proposal\b|recommendation\b|recommend\b|"
    r"open question\b|question\b|consider\b"
    r")",
    re.IGNORECASE,
)

# Minimum enumerated lines for the multi-deliverable scope this gate targets.
_MIN_DELIVERABLES: Final[int] = 2


def _is_recommendation_prose(text: str, bodies: list[str]) -> bool:
    """True when the turn is architecture/planning/recommendation prose, not a claim.

    Precision-preserving AND of two independent signals, so a genuine
    multi-deliverable done-claim is never exempted: (1) the turn carries a
    recommendation/proposal/options FRAME (``_RECOMMENDATION_FRAME_RE``), AND
    (2) a MAJORITY of the enumerated lines are option/pattern/decision/question
    lines (``_RECOMMENDATION_LINE_RE``), i.e. the enumeration is options to
    choose among, not delivered work. The real-incident stranded claim
    ("- Backend change: MR opened") carries NO recommendation frame and ZERO
    option-shaped lines, so it stays firing.
    """
    if not bodies or not _RECOMMENDATION_FRAME_RE.search(text):
        return False
    reco_lines = sum(1 for body in bodies if _RECOMMENDATION_LINE_RE.match(body))
    return reco_lines * 2 >= len(bodies)


class CompletionVerdict(NamedTuple):
    """Why a completion claim was blocked, for the handler's BLOCK reason.

    ``missing`` lists the human-readable reasons a deliverable->evidence map is
    incomplete (a deliverable with no on-target evidence, the unread spec, the
    unverified crucial surface) so the handler can name them in the block.
    """

    deliverable_count: int
    missing: list[str]


def _has_completeness_claim(text: str) -> bool:
    """True when ``text`` asserts the whole of the work is done/clean."""
    return bool(_COMPLETENESS_CLAIM_RE.search(text))


def _is_honest_refusal(text: str) -> bool:
    """True when ``text`` is the desired "NOT done: <X> stranded" refusal."""
    return bool(_NOT_DONE_REFUSAL_RE.search(text))


def _deliverable_bodies(text: str) -> list[str]:
    """The body text of every enumerated deliverable line, in order."""
    return [m.group("body").strip() for m in _DELIVERABLE_LINE_RE.finditer(text)]


def _line_has_on_target_evidence(body: str) -> bool:
    """True when one enumerated line carries on-target evidence, not a proxy.

    A line whose only signal is "an MR exists / PR opened" is NOT on-target
    (the issue forbids the artifact-existence proxy), even if some on-target
    phrase also appears: the explicit on-target anchor must be present AND not
    be merely the artifact-existence proxy.
    """
    return bool(_ON_TARGET_EVIDENCE_RE.search(body))


def _no_claim_to_evaluate(text: str) -> bool:
    """True when there is no completeness claim to evaluate before line parsing.

    Folds the three text-only pre-checks — empty text, no completeness verb, or
    an honest "NOT done" refusal — so the main detector stays within the
    return-count budget without a suppression.
    """
    return not text or not _has_completeness_claim(text) or _is_honest_refusal(text)


def find_completion_block(text: str) -> CompletionVerdict | None:
    """Return a verdict to BLOCK, or ``None`` to allow (no fire).

    Fires (returns a verdict) ONLY when ALL hold: the turn carries a
    completeness assertion AND is not an honest refusal; the turn enumerates
    at least ``_MIN_DELIVERABLES`` distinct deliverables (multi-deliverable
    scope — a single-deliverable claim never fires); and the
    deliverable->evidence map is INCOMPLETE — some enumerated deliverable
    lacks on-target evidence, OR the authoritative spec was not read, OR the
    crucial deliverable was not explicitly verified on its correct surface.

    Returns ``None`` (allow) when there is no claim, when it is an honest
    refusal, when the turn is architecture/planning/recommendation prose
    enumerating options rather than delivered work, when the ticket is
    single-deliverable, or when every leg of the map is satisfied. Empty/odd
    input yields ``None``.
    """
    if _no_claim_to_evaluate(text):
        return None
    bodies = _deliverable_bodies(text)
    if len(bodies) < _MIN_DELIVERABLES:
        return None
    if _is_recommendation_prose(text, bodies):
        return None
    missing: list[str] = []
    lines_without_evidence = [
        body for body in bodies if not _line_has_on_target_evidence(body) or _ARTIFACT_EXISTS_RE.search(body)
    ]
    if lines_without_evidence:
        missing.append(
            f"{len(lines_without_evidence)} of {len(bodies)} deliverables lack on-target evidence "
            "(an MR/PR existing is not evidence — needs merged-to-target / verified-on-surface / passing-E2E)"
        )
    if not _SPEC_READ_RE.search(text):
        missing.append("the authoritative spec (incl. its comments) was not confirmed read")
    if not _CRUCIAL_SURFACE_RE.search(text):
        missing.append("the crucial deliverable was not explicitly verified on its correct surface")
    if not missing:
        return None
    return CompletionVerdict(deliverable_count=len(bodies), missing=missing)


def format_block_message(verdict: CompletionVerdict) -> str:
    """Render the BLOCK reason naming the incomplete legs of the map."""
    reasons = "\n".join(f"  - {reason}" for reason in verdict.missing)
    return (
        f"COMPLETION-CLAIM GATE (#2665) — this turn claims the work is done on a "
        f"multi-deliverable ticket ({verdict.deliverable_count} deliverables enumerated) "
        "but the deliverable->evidence map is incomplete:\n"
        f"{reasons}\n"
        "Do NOT claim done. Produce a complete deliverable->evidence table where EVERY "
        "spec deliverable (incl. the spec's comments) has concrete evidence ON the merge "
        "target (merged to target / verified on the correct surface / passing E2E), the "
        "crucial deliverable is explicitly verified on its surface, and confirm the "
        "authoritative spec was read. If any deliverable lacks on-target evidence, say "
        "'NOT done: <X> missing / on the wrong surface / stranded off target'."
    )
