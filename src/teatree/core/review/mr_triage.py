"""The pure MR-triage decision core: MR facts in, exactly one triage action out.

No I/O — no forge call, no Slack post, no Django, no clock. Every input arrives
on :class:`MrFacts` (the caller does the reading and the arithmetic) and the only
output is a :class:`TriageVerdict`, so the policy is testable as a table and the
transport layer owns nothing but transport.

:data:`_LADDER` is the policy, first match wins. Order is load-bearing: red CI
outranks EVERY other rung, a draft and a review-exempt repo included, because
every merge request owes green CI whatever else is true of it — so a red draft
reports :attr:`TriageAction.FIX_CI` rather than sitting broken in silence, and a
red exempt MR does too. Below red CI a draft leaves triage entirely, review
exemption answers for everything social (an exempt repo owes no review request),
and the repo-fit escalation plus the work-group hold outrank both the
review-request and the nag rungs, so an MR whose disposition needs a judgement
call — or whose siblings are not ready — is never auto-pinged.
A fact set matching no rung falls to :attr:`TriageAction.ASK_OWNER` — the ladder
has no silent no-op, so an MR can never be dropped by a gap in the policy.

Threshold provenance (:class:`TriageThresholds`). ``engineering_nag`` is DERIVED:
it is the observed re-ping cadence, and the same 2 days
:mod:`teatree.loop.scanners.review_nag` already enforces. ``devops_nag`` and
``stale_no_review`` are INFERRED — the observed data BOUNDS them (DevOps was left
longer than the engineering interval; an MR was drafted well before a fortnight)
without fixing them — so both are named, overridable config settings rather than
constants baked into the ladder. ``nag_backoff_cap`` is CHOSEN: nothing in the
data fixes where re-asking should stop widening, so it is the operator's
``review_nag_max_interval_days``, resolved by the transport and passed in.

:func:`fibonacci_step` is the one import here, and it carries no I/O either —
this module multiplies by the sequence, it does not go and get one.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from teatree.core.modelkit.fibonacci import fibonacci_step

HEDGED_REVIEW_REQUEST_NOTE = "not sure if this is worth it... WDYT?"


class RepoOwner(StrEnum):
    """Which org function reviews a repo — the input that picks the nag patience.

    DevOps is the PATIENT owner: an infrastructure MR waits on a rota with its own
    cadence, so nagging it on the engineering interval is noise. Every "we do not
    know who owns this repo" path resolves here, never to ENGINEERING.
    """

    ENGINEERING = "engineering"
    DEVOPS = "devops"


class CiState(StrEnum):
    """The MR's required-checks verdict, plus the honest "we did not read it" state.

    UNKNOWN is not a failure and not a pass: it is what a bounded enrichment pass
    leaves behind when it did not reach this MR. It never dispatches a CI fix, and
    it never satisfies a rung that needs a confirmed green.
    """

    GREEN = "green"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReviewRequestState(StrEnum):
    """Whether anyone has been asked to review — tri-state, defaulting to UNKNOWN.

    NONE and REQUESTED select the two social rungs; UNKNOWN selects neither, so an
    unresolved review-request signal surfaces as an owner question instead of a
    guess in either direction.
    """

    UNKNOWN = "unknown"
    NONE = "none"
    REQUESTED = "requested"


class TriageAction(StrEnum):
    NONE = "none"
    FIX_CI = "fix_ci"
    FLAG_STACKED_ON_UNREVIEWED_BASE = "flag_stacked_on_unreviewed_base"
    ESCALATE_MODEL = "escalate_model"
    WAIT = "wait"
    PROPOSE_DRAFT = "propose_draft"
    REQUEST_REVIEW = "request_review"
    GROUP_PING = "group_ping"
    ASK_OWNER = "ask_owner"


class TriageReason(StrEnum):
    """Which rung fired.

    Several rungs share one :class:`TriageAction` — three of them answer
    :attr:`TriageAction.WAIT` alone — so the reason, never the action, is what
    tells a work-group hold from a pipeline still running or a nag window still
    open.
    """

    DRAFT = "draft"
    CI_FAILED = "ci_failed"
    REVIEW_EXEMPT = "review_exempt"
    STACKED_ON_UNREVIEWED_BASE = "stacked_on_unreviewed_base"
    REPO_FIT = "repo_fit"
    WORK_GROUP_NOT_READY = "work_group_not_ready"
    CI_NOT_GREEN = "ci_not_green"
    STALE_NO_REVIEW = "stale_no_review"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    WITHIN_NAG_INTERVAL = "within_nag_interval"
    NAG_INTERVAL_EXCEEDED = "nag_interval_exceeded"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class MrFacts:
    """Everything the ladder is allowed to know about one MR.

    Defaults are the fail-safe end of each axis: an un-populated fact set is
    ``UNKNOWN`` CI, ``UNKNOWN`` review-request and the patient repo owner, so a
    caller that forgets to fill a field gets an owner question rather than a
    confident wrong action.

    ``repo_fit_tripwire``, ``work_group`` and ``work_group_unready_members`` are
    pre-computed by the caller: the core stays free of content analysis and of
    any cross-MR query, and carries only the resulting key and count.

    ``review_exempt`` is likewise pre-computed — by
    :func:`teatree.core.review.repo_exemption.is_review_exempt`, against declared
    patterns. It defaults False, so a caller that never resolved it triages the
    MR normally rather than silently skipping its review request.
    """

    url: str = ""
    repo_owner: RepoOwner = RepoOwner.DEVOPS
    draft: bool = False
    review_exempt: bool = False
    ci: CiState = CiState.UNKNOWN
    review_request: ReviewRequestState = ReviewRequestState.UNKNOWN
    approved: bool = False
    age_since_opened: dt.timedelta = dt.timedelta()
    idle_since_review_requested: dt.timedelta = dt.timedelta()
    target_branch: str = ""
    repo_default_branch: str = ""
    base_review_request: ReviewRequestState = ReviewRequestState.UNKNOWN
    repo_fit_tripwire: str = ""
    work_group: str = ""
    work_group_unready_members: int = 0
    author_unsure: bool = False

    @property
    def targets_repo_default(self) -> bool:
        return self.target_branch == self.repo_default_branch


@dataclass(frozen=True, slots=True)
class TriageThresholds:
    """The durations the ladder reads, and the re-ask backoff built on them.

    See the module docstring for the provenance of each. ``nag_backoff_cap``
    mirrors the ``review_nag_max_interval_days`` setting the transport resolves.
    """

    engineering_nag: dt.timedelta = dt.timedelta(days=2)
    devops_nag: dt.timedelta = dt.timedelta(days=5)
    stale_no_review: dt.timedelta = dt.timedelta(days=7)
    nag_backoff_cap: dt.timedelta = dt.timedelta(days=30)

    def nag_interval(self, owner: RepoOwner) -> dt.timedelta:
        return self.engineering_nag if owner is RepoOwner.ENGINEERING else self.devops_nag

    def nag_interval_for_attempt(self, owner: RepoOwner, attempt: int) -> dt.timedelta:
        """How long the *attempt*-th re-ask waits: the owner's base, widened, capped.

        The Fibonacci step MULTIPLIES the per-owner interval rather than
        replacing it, so an engineering repo widens 2, 2, 4, 6, 10, 16, 26 days
        while DevOps widens 5, 5, 10, 15, 25 — the patience distinction survives
        the backoff instead of being flattened into one shared curve.
        """
        return min(self.nag_interval(owner) * fibonacci_step(attempt), self.nag_backoff_cap)

    def nag_backoff_at_cap(self, owner: RepoOwner, attempt: int) -> bool:
        """Whether *attempt* has run the schedule out to its ceiling.

        Compares the UNCLIPPED product, because
        :meth:`nag_interval_for_attempt` has already erased the overshoot — a
        caller asking "is there any widening left?" cannot tell the last honest
        step from the first clipped one by reading the interval back.
        """
        return self.nag_interval(owner) * fibonacci_step(attempt) >= self.nag_backoff_cap


DEFAULT_THRESHOLDS = TriageThresholds()


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    """One action, the rung that produced it, and the rung's own free-text payload.

    ``detail`` is whatever the firing rung needs to hand its consumer — the
    tripwire text, the work-group key, the author's hedge — never a rendered
    message for a specific transport.
    """

    action: TriageAction
    reason: TriageReason
    detail: str = ""


type _Rung = Callable[[MrFacts, TriageThresholds], TriageVerdict | None]


def _draft_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    del thresholds
    if not facts.draft:
        return None
    return TriageVerdict(action=TriageAction.NONE, reason=TriageReason.DRAFT)


def _failed_ci_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    del thresholds
    if facts.ci is not CiState.FAILED:
        return None
    return TriageVerdict(action=TriageAction.FIX_CI, reason=TriageReason.CI_FAILED)


def _review_exempt_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    del thresholds
    if not facts.review_exempt:
        return None
    return TriageVerdict(action=TriageAction.NONE, reason=TriageReason.REVIEW_EXEMPT)


def _stacked_base_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    del thresholds
    if facts.targets_repo_default or facts.base_review_request is not ReviewRequestState.NONE:
        return None
    return TriageVerdict(
        action=TriageAction.FLAG_STACKED_ON_UNREVIEWED_BASE,
        reason=TriageReason.STACKED_ON_UNREVIEWED_BASE,
        detail=facts.target_branch,
    )


def _repo_fit_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    del thresholds
    if not facts.repo_fit_tripwire:
        return None
    return TriageVerdict(
        action=TriageAction.ESCALATE_MODEL,
        reason=TriageReason.REPO_FIT,
        detail=facts.repo_fit_tripwire,
    )


def _work_group_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    """Hold a member whose work group still has an unready sibling.

    A unit of work that arrived as three merge requests is reviewed as three or
    not at all, so the whole group waits together: releasing this one now hands a
    reviewer a fragment and interrupts them again as each sibling lands.
    """
    del thresholds
    if not facts.work_group or facts.work_group_unready_members < 1:
        return None
    return TriageVerdict(
        action=TriageAction.WAIT,
        reason=TriageReason.WORK_GROUP_NOT_READY,
        detail=facts.work_group,
    )


def _unrequested_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    if facts.review_request is not ReviewRequestState.NONE:
        return None
    if facts.ci is not CiState.GREEN:
        return TriageVerdict(action=TriageAction.WAIT, reason=TriageReason.CI_NOT_GREEN)
    if facts.age_since_opened > thresholds.stale_no_review:
        return TriageVerdict(action=TriageAction.PROPOSE_DRAFT, reason=TriageReason.STALE_NO_REVIEW)
    return TriageVerdict(
        action=TriageAction.REQUEST_REVIEW,
        reason=TriageReason.READY_FOR_REVIEW,
        detail=HEDGED_REVIEW_REQUEST_NOTE if facts.author_unsure else "",
    )


def _requested_rung(facts: MrFacts, thresholds: TriageThresholds) -> TriageVerdict | None:
    if facts.review_request is not ReviewRequestState.REQUESTED:
        return None
    if facts.approved:
        return TriageVerdict(action=TriageAction.NONE, reason=TriageReason.APPROVED)
    if facts.idle_since_review_requested < thresholds.nag_interval(facts.repo_owner):
        return TriageVerdict(action=TriageAction.WAIT, reason=TriageReason.WITHIN_NAG_INTERVAL)
    return TriageVerdict(action=TriageAction.GROUP_PING, reason=TriageReason.NAG_INTERVAL_EXCEEDED)


_LADDER: tuple[_Rung, ...] = (
    _failed_ci_rung,
    _draft_rung,
    _review_exempt_rung,
    _stacked_base_rung,
    _repo_fit_rung,
    _work_group_rung,
    _unrequested_rung,
    _requested_rung,
)


def triage(facts: MrFacts, *, thresholds: TriageThresholds = DEFAULT_THRESHOLDS) -> TriageVerdict:
    """Walk :data:`_LADDER` in order and return the first rung that fires."""
    for rung in _LADDER:
        verdict = rung(facts, thresholds)
        if verdict is not None:
            return verdict
    return TriageVerdict(action=TriageAction.ASK_OWNER, reason=TriageReason.INDETERMINATE)
