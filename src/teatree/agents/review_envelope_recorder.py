"""Record a reviewing task's returned ``review_verdict`` envelope server-side (corr-11).

The orchestrator half of the headless review lane, split out of
:mod:`teatree.agents.attempt_recorder` so the verdict seam has its own home. A
reviewer RETURNS a typed verdict rather than writing the row itself — maker≠checker
reserves the write for a different actor — and this module is that actor: it binds the
verdict to the head the review was DISPATCHED for, records it, and reports a refusal as
a string the caller turns into a failed task.
"""

import dataclasses
from typing import TYPE_CHECKING, cast

from django.db import transaction

from teatree.agents.envelope_refusal import (
    MALFORMED_REVIEW_VERDICT_PREFIX,
    MALFORMED_RUBRIC_GRADES_PREFIX,
    review_envelope_shape_error,
)
from teatree.agents.fix_record_recorder import fix_record_refusal
from teatree.agents.result_schema import AgentResultBlob, ReviewVerdictEnvelope
from teatree.agents.review_context_recorder import anti_vacuity_refusal
from teatree.agents.review_nit_policy import normalize_review_envelope
from teatree.agents.review_rubric_recorder import ReviewGrading, merge_safe_over_fail_error, rubric_coverage_error
from teatree.core.gates.integration_review_gate import distinct_repos
from teatree.core.gates.rubric_gate import clear_honesty_escalation_on_pass
from teatree.core.merge.ticket_resolution import gated_ticket_for_review_task
from teatree.core.modelkit.phase_tools import ENVELOPE_VERDICT_PHASES
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.project_repo_identity import project_repo_identity_for_ticket
from teatree.core.modelkit.task_failure_taxonomy import (
    REVIEW_UNRECORDABLE_PREFIX,
    VENUE_LIMIT_PREFIX,
    WRONG_HEAD_RETRY_PREFIX,
)
from teatree.core.models import (
    ChecksContradictionError,
    DeferredQuestion,
    Finding,
    ReviewEvidence,
    ReviewHoldDisposition,
    ReviewHoldDispositionError,
    ReviewVerdict,
    ReviewVerdictError,
    RoundMetric,
    Rubric,
    RubricError,
    Task,
)
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS, AutoReviewDispatch
from teatree.core.models.review_evidence import ReviewEvidenceError
from teatree.core.models.review_target import (
    MOVED_REVIEW_RETRY_HEADS_KEY,
    ReviewTarget,
    review_target_for_task,
    unattachable_verdict_refusal,
    verdict_at,
)
from teatree.core.models.reviewer_identity import (
    assigned_reviewer_identity,
    is_independent_reviewer_identity,
    is_non_reviewer_role,
)
from teatree.core.review.diff_scope_probe import changed_file_set_for_findings
from teatree.core.review.head_workflow_runs import live_checks_at
from teatree.core.review.live_head import live_head_at
from teatree.core.review.nit_lane import NitLaneError, validate_nit_comments
from teatree.core.review.nit_publish import pr_url_for_nits, publish_nits, review_nits
from teatree.core.review.review_plan import plan_for_task
from teatree.core.review.verdict_head_binding import resolve_verdict_head
from teatree.utils.pr_ref import PrRef


def review_head_mismatch_refusal(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    if "review_head_mismatch" not in result:
        return ""
    if normalize_phase(phase or task.phase) not in ENVELOPE_VERDICT_PHASES or "review_verdict" in result:
        return "review_head_mismatch requires a reviewing phase without a verdict"
    mismatch = result["review_head_mismatch"]
    if not isinstance(mismatch, dict) or not all(
        isinstance(mismatch.get(key), str) and mismatch[key].strip() for key in ("observed_sha", "reason")
    ):
        return "review_head_mismatch requires observed_sha and reason"
    target = review_target_for_task(task)
    if target is None:
        return unattachable_verdict_refusal(task)
    live = live_head_at(slug=target.slug, pr_id=target.pr_id, host_kind=target.host_kind)
    if live.unreadable or not live.sha:
        return "outage_death: review head mismatch; live pull request head could not be read"
    return f"{WRONG_HEAD_RETRY_PREFIX}{live.sha}; checkout resolved to {mismatch['observed_sha']}: {mismatch['reason']}"


def review_venue_limit_refusal(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    if "review_venue_limit" not in result:
        return ""
    if normalize_phase(phase or task.phase) not in ENVELOPE_VERDICT_PHASES or "review_verdict" in result:
        return "review_venue_limit requires a reviewing phase without review_verdict"
    limit = result["review_venue_limit"]
    reasons = limit.get("limits") if isinstance(limit, dict) else None
    if (
        not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(reason, str) and reason.strip() for reason in reasons)
    ):
        return "review_venue_limit requires nonempty limits"
    return VENUE_LIMIT_PREFIX + "; ".join(reasons)


def settle_review_venue_limit(task: Task, error: str) -> None:
    if not error.startswith(VENUE_LIMIT_PREFIX):
        return
    target = review_target_for_task(task)
    if target is None or not target.head_sha:
        return
    ticket = gated_ticket_for_review_task(task) or task.ticket
    RoundMetric.objects.settle(
        kind=RoundMetric.Kind.REVIEW, ticket=ticket, subject=target.head_sha, outcome="venue_limit"
    )


if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import FixRecord, RubricGrade


#: Codex phases record through the shell; headless and E2E envelopes use this seam.
_REVIEW_VERDICT_PHASES = frozenset({"reviewing", "e2e_reviewing"})

#: Verdict-recording phases that also OWE rubric coverage — the ones whose brief carries
#: the checklist (``phase_blocks._reviewing_phase_lines`` injects ``rubric_brief_lines``).
#: Only the headless reviewing brief supplies rubric criteria.
_RUBRIC_GRADED_PHASES = ENVELOPE_VERDICT_PHASES
_MIN_INTEGRATION_REPOS = 2


def _additional_evidence_error(
    ticket: "Ticket | None", envelope: ReviewVerdictEnvelope, result: AgentResultBlob
) -> str:
    """Validate the evidence a merge-safe review must return for the owning ticket."""
    if ticket is None or str(envelope.get("verdict", "")).strip().lower() != ReviewVerdict.Verdict.MERGE_SAFE:
        return ""
    if refusal := anti_vacuity_refusal(result.get("anti_vacuity")):
        return refusal
    repos = distinct_repos(ticket)
    if len(repos) < _MIN_INTEGRATION_REPOS:
        return ""
    integration_review = result.get("integration_review")
    covered = integration_review.get("repos") if isinstance(integration_review, dict) else None
    if (
        not isinstance(covered, list)
        or not all(isinstance(repo, str) for repo in covered)
        or not project_repo_identity_for_ticket(ticket).covers(repos, covered)
    ):
        return (
            "integration review recording refused: review the combined changeset and return "
            "every ticket repo in integration_review.repos"
        )
    return ""


def record_returned_review_envelope(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    """Record a reviewing task's returned ``review_verdict`` server-side (corr-11).

    The orchestrator half of the headless review lane: a Bash-denied reviewer
    RETURNS a typed ``review_verdict``; this records the ``ReviewVerdict`` (which
    resolves the per-MR :class:`MRReviewLock`) — maker≠checker holds because THIS
    actor is not the author. Returns an error string when the verdict is malformed, the
    reviewer identity is a maker/loop role, the reviewer's self-asserted head diverges
    from the dispatch head, the returned ``rubric_grades`` leave a criterion ungraded, or
    the recorded row is unreachable by read-back (the caller fails the task so the block
    surfaces), else ``""``.

    The reviewer is ALSO the rubric's only automatic grader: it is the independent
    verifier the done-gate requires, and it is the one actor that has read the tree. So
    the verdict and the grades land in ONE transaction — see :func:`_record_verdict_and_grades`
    for why that ordering is load-bearing.

    A non-reviewing phase, or a result without a ``review_verdict``, is a no-op. So is an
    author's pre-PR self-review, whose verdict stays on the task attempt. Any other verdict
    that reaches no pull request, or cannot persist on the one it reaches, fails loudly (#4308).
    """
    resolved_phase = normalize_phase(phase or task.phase)
    envelope = _returned_review_verdict(result, phase=resolved_phase)
    if envelope is None:
        return ""
    if shape_error := review_envelope_shape_error(envelope):
        return f"{MALFORMED_REVIEW_VERDICT_PREFIX}{shape_error}"
    target, refusal = _bound_review_target(task, envelope)
    if target is None:
        return refusal

    ticket = gated_ticket_for_review_task(task) if resolved_phase in _RUBRIC_GRADED_PHASES else None
    rubric = Rubric.objects.active_for_ticket(ticket) if ticket is not None else None
    envelope = normalize_review_envelope(envelope, ticket)
    result["review_verdict"] = envelope
    plan = plan_for_task(task, create=False)
    carried = plan.valid_carried(task, target.head_sha) if plan is not None else frozenset()
    evidence_error = _additional_evidence_error(ticket, envelope, result)
    if evidence_error:
        return evidence_error
    returned = envelope.get("rubric_grades")
    unverified = str(envelope.get("verdict", "")).strip().lower() == ReviewVerdict.Verdict.UNVERIFIED
    try:
        # Scoped to a rubric that EXISTS, like both refusals below — with none, nothing is stamped.
        grades: list[RubricGrade] = [] if rubric is None or unverified else Rubric.normalize_grades(returned)
    except RubricError as exc:
        return f"{MALFORMED_RUBRIC_GRADES_PREFIX}{exc}"
    return (
        ("" if unverified else rubric_coverage_error(rubric, grades, returned=returned, carried=carried))
        or merge_safe_over_fail_error(rubric, envelope, grades)
        or _nit_comment_error(envelope, ticket or task.ticket, target.head_sha)
        or _record_verdict_and_grades(
            task,
            result,
            envelope=envelope,
            target=target,
            grading=ReviewGrading(rubric, grades, carried, plan.delta.prior_head if plan is not None else ""),
        )
        or _settle_recorded_verdict(task, target, rubric=rubric, outcome=str(envelope.get("verdict", "")))
    )


def _bound_review_target(task: Task, envelope: ReviewVerdictEnvelope) -> tuple[ReviewTarget | None, str]:
    """The PR + head *task*'s verdict binds to, or ``None`` with the refusal (``""`` for a pre-PR self-review)."""
    target = review_target_for_task(task)
    if target is None:
        return None, unattachable_verdict_refusal(task)
    if not target.head_sha and not target.bind_live_head:
        return None, (
            f"{REVIEW_UNRECORDABLE_PREFIX}review verdict cannot be persisted: this review is answerable for "
            f"{target.slug}#{target.pr_id} but no pull request head is recorded for it, so the "
            f"verdict would bind to no tree and no merge guard could ever read it"
        )
    binding = resolve_verdict_head(
        asserted=str(envelope.get("reviewed_sha") or "").strip(),
        dispatch_head=target.head_sha,
        pr=PrRef(slug=target.slug, pr_id=target.pr_id, host_kind=target.host_kind),
    )
    if binding.error:
        if binding.superseded:
            with transaction.atomic():
                _supersede_moved_head(target)
                if binding.retry_head and target.armed_by is AutoReviewDispatch:
                    task.ticket.merge_extra(
                        merge_into_dicts={MOVED_REVIEW_RETRY_HEADS_KEY: {binding.retry_head: str(task.pk)}}
                    )
        elif binding.retry_head:
            return None, f"{WRONG_HEAD_RETRY_PREFIX}{binding.retry_head}; {binding.error}"
        return None, binding.error
    return dataclasses.replace(target, head_sha=binding.head), ""


def _recorded_reviewer_identity(target: ReviewTarget, envelope: "ReviewVerdictEnvelope") -> str:
    """Which identity the verdict lands under — the returned one, or the dispatch's (#2663).

    A returned identity the gate cannot admit is REPLACED, not refused: the dispatch already
    named one, so discarding a finished review over the agent's spelling buys nothing (22
    attempts and $104 in one billing cycle, 17 of them the single word ``claude:review``).
    An admitted identity is kept verbatim, so two genuine reviewers at one head stay two rows
    and a second reviewer cannot overwrite the first one's hold. A maker/review-authoring role
    is kept too — that is the agent declaring itself the author, which no dispatch may overrule,
    and ``ReviewVerdict.record`` still refuses it.
    """
    returned = str(envelope.get("reviewer_identity") or "").strip()
    if returned and (is_independent_reviewer_identity(returned) or is_non_reviewer_role(returned)):
        return returned
    return assigned_reviewer_identity(target.pr_id)


def _record_verdict_and_grades(
    task: Task,
    result: AgentResultBlob,
    *,
    envelope: ReviewVerdictEnvelope,
    target: ReviewTarget,
    grading: ReviewGrading,
) -> str:
    """Write the verdict and the rubric grades in ONE transaction, or return the refusal.

    The single atomic is the point. ``ReviewVerdict.record`` retires the per-head claim
    and releases the review lock alongside the row, so a grade refused AFTER it would
    leave a verdict standing over an ungradeable rubric with nothing left to re-arm.
    Rolling all four back together is what keeps a refused grade re-dispatchable.

    The grader identity and the SHA are the verdict's OWN — the same string the row is
    recorded under, and the head the verdict BOUND to (the dispatch head, or the live head
    the branch advanced to) rather than the reviewer's raw assertion, so a grade can never
    vouch for a tree the verdict does not.
    """
    fix_record = result.get("fix_record")
    ticket = gated_ticket_for_review_task(task)
    if fix_record is not None and (refusal := fix_record_refusal(fix_record, ticket=ticket)):
        return refusal
    merge_safe = str(envelope.get("verdict", "")).strip().lower() == ReviewVerdict.Verdict.MERGE_SAFE
    unverified = str(envelope.get("verdict", "")).strip().lower() == ReviewVerdict.Verdict.UNVERIFIED
    raw_findings = envelope.get("findings", [])
    findings = (
        [Finding.from_dict(item) for item in raw_findings if isinstance(item, dict)]
        if isinstance(raw_findings, list)
        else []
    )
    identity = _recorded_reviewer_identity(target, envelope)
    anti_vacuity = result.get("anti_vacuity")
    integration_review = result.get("integration_review")
    try:
        with transaction.atomic():
            if ticket is not None and merge_safe and isinstance(anti_vacuity, dict):
                ticket.record_anti_vacuity_attestation(
                    target.head_sha,
                    str(anti_vacuity.get("ac_coverage", "")),
                    [str(node) for node in anti_vacuity.get("proven_tests", [])],
                    no_new_tests=anti_vacuity.get("no_new_tests") is True,
                )
            if ticket is not None and merge_safe and fix_record is not None:
                ticket.record_fix_record(cast("FixRecord", fix_record))
            recorded = ReviewVerdict.record(
                pr_id=target.pr_id,
                slug=target.slug,
                reviewed_sha=target.head_sha,
                verdict=str(envelope.get("verdict", "")),
                reviewer_identity=identity,
                findings=findings,
                evidence_gaps=envelope.get("evidence_gaps"),
                gh_verify_result=str(envelope.get("gh_verify_result") or "green"),
                blast_class=str(envelope.get("blast_class") or "logic"),
                ticket=ticket or task.ticket,
                lock_holder=target.lock_holder,
                changed_files=changed_file_set_for_findings(findings, slug=target.slug, pr_id=target.pr_id),
                merge_result_retake=bool(envelope.get("merge_result_retake")),
                live_checks=live_checks_at,
            )
            _require_verdict_read_back(target)
            _record_hold_dispositions(recorded, envelope)
            if (
                ticket is not None
                and len(distinct_repos(ticket)) >= _MIN_INTEGRATION_REPOS
                and merge_safe
                and isinstance(integration_review, dict)
            ):
                ReviewEvidence.record(
                    ticket=ticket,
                    kind=ReviewEvidence.Kind.INTEGRATION_REVIEW,
                    reviewer_identity=identity,
                    verdict=str(envelope.get("verdict", "")),
                    head_sha=target.head_sha,
                    repos=[str(repo) for repo in integration_review.get("repos", [])],
                )
            if not unverified:
                grading.apply(identity=identity, head=target.head_sha)
    except (ReviewVerdictError, NitLaneError, ReviewHoldDispositionError, ReviewVerdict.DoesNotExist) as exc:
        # The one refusal class a re-dispatch can never satisfy at this head is latched;
        # every other one names something the next reviewer could get right, so it keeps
        # the ordinary retry. The discrimination is the EXCEPTION TYPE — the raise site
        # itself — never the message text a reword would detach this from.
        if isinstance(exc, ChecksContradictionError):
            _latch_checks_contradiction(target, task=task, reason=str(exc))
        return f"review verdict recording refused: {exc}"
    except RubricError as exc:
        return f"{MALFORMED_RUBRIC_GRADES_PREFIX}{exc}"
    except ReviewEvidenceError as exc:
        return f"integration review recording refused: {exc}"
    return _publish_review_nits_with_recovery(ticket or task.ticket, target, findings)


def _record_hold_dispositions(recorded: ReviewVerdict, envelope: ReviewVerdictEnvelope) -> None:
    responses = envelope.get("hold_dispositions", [])
    if responses and recorded.verdict != ReviewVerdict.Verdict.MERGE_SAFE:
        msg = "hold dispositions require a merge-safe verdict"
        raise ReviewHoldDispositionError(msg)
    if recorded.verdict == ReviewVerdict.Verdict.MERGE_SAFE:
        standing = ReviewVerdict.objects.unreconciled_holds_at(
            slug=recorded.slug, pr_id=recorded.pr_id, head_sha=recorded.reviewed_sha
        )
        missing = {hold.pk for hold in standing} - {response["hold_id"] for response in responses}
        if missing:
            msg = f"merge-safe review leaves standing HOLD verdicts unanswered: {sorted(missing)}"
            raise ReviewHoldDispositionError(msg)
    for response in responses:
        hold = ReviewVerdict.objects.get(pk=response["hold_id"])
        ReviewHoldDisposition.record(hold=hold, clearing_verdict=recorded, points=response["points"])


def _publish_review_nits_with_recovery(ticket: "Ticket", target: ReviewTarget, findings: list[Finding]) -> str:
    if not (points := review_nits(findings)):
        return ""
    ticket.merge_extra(merge_into_dicts={"nit_publication": {target.head_sha: {"status": "pending"}}})
    try:
        publish_nits(ticket, _nit_pr_url(ticket, target.head_sha), target.head_sha, points)
    except NitLaneError as exc:
        ticket.merge_extra(
            merge_into_dicts={"nit_publication": {target.head_sha: {"status": "failed", "error": str(exc)}}}
        )
        return f"review NIT publication refused: {exc}"
    ticket.merge_extra(merge_into_dicts={"nit_publication": {target.head_sha: {"status": "published"}}})
    return ""


def _nit_pr_url(ticket: "Ticket", head_sha: str) -> str:
    pr_url = pr_url_for_nits(ticket, head_sha)
    if not pr_url:
        message = "review NIT has no attributable pull request URL"
        raise NitLaneError(message)
    return pr_url


def _nit_comment_error(envelope: ReviewVerdictEnvelope, ticket: "Ticket", head_sha: str) -> str:
    raw = envelope.get("findings")
    findings = [Finding.from_dict(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    points = review_nits(findings)
    try:
        validate_nit_comments(points, pr_url=pr_url_for_nits(ticket, head_sha) if points else "")
    except NitLaneError as exc:
        return f"review verdict recording refused: {exc}"
    return ""


def _settle_recorded_verdict(task: Task, target: ReviewTarget, *, rubric: "Rubric | None", outcome: str) -> str:
    """After a recorded verdict: rebind the claim, clear a satisfied escalation, and prove the row reads back."""
    _rebind_claim_to_recorded_head(task, target)
    if rubric is not None and rubric.is_fully_passed_at(target.head_sha):
        clear_honesty_escalation_on_pass(rubric.ticket)
    error = _unpersisted_verdict_error(target)
    if not error:
        RoundMetric.objects.settle(
            kind=RoundMetric.Kind.REVIEW,
            ticket=gated_ticket_for_review_task(task) or task.ticket,
            subject=target.head_sha,
            outcome=outcome,
        )
    return error


def _supersede_moved_head(target: ReviewTarget) -> None:
    """Retire the claim for a head the PR has advanced past, so review re-arms at the new one.

    Scoped to the #68 dispatch ledger on purpose: it is the only per-head claim the PR sweep
    re-arms, so superseding a codex marker would release a review lock nothing re-takes.
    """
    if target.armed_by is not AutoReviewDispatch:
        return
    AutoReviewDispatch.mark_superseded(slug=target.slug, pr_id=target.pr_id, head_sha=target.claim_head_sha)


def _rebind_claim_to_recorded_head(task: Task, target: ReviewTarget) -> None:
    """Point whatever key the resolver reads at the head the verdict landed on; no-op when unmoved.

    ``ReviewVerdict.record`` retires the claim keyed on the RECORDED head, which is not the
    pinned one once the branch advanced — so without this the spent claim stays in flight and
    the landed-work guard keeps looking up a tree nobody ended up reviewing.

    Both resolver keys are stamped, because :func:`review_target_for_task` reads a different
    one on each path and only the dispatch path carried a writer: on the 1454 of 2152
    verdict-review tasks holding no dispatch row the verdict landed at the live head while
    ``extra["reviewed_sha"]`` still named the pinned one, so the resolver re-read a tree the
    row is not on and the system could not find its own verdict.
    """
    if target.head_sha == target.claim_head_sha or target.bind_live_head:
        return
    if target.armed_by is AutoReviewDispatch:
        AutoReviewDispatch.mark_recorded_at(
            slug=target.slug,
            pr_id=target.pr_id,
            head_sha=target.claim_head_sha,
            recorded_head_sha=target.head_sha,
        )
        return
    task.ticket.merge_extra(set_keys={"reviewed_sha": target.head_sha})


#: Prefix of the refusal question's dedupe marker, per head. Distinct from
#: ``transient_requeue``'s ticket-agnostic ``repair-halt:`` marker on purpose: that one
#: collapses every deterministic halt sharing a failure fingerprint into ONE question, so
#: any other pull request contradicting its own checks would be silently folded into the
#: first one's page. The head IS the subject here, and each one needs its own answer.
_REFUSAL_MARKER_PREFIX = "review-refusal:"

#: How much of the head the marker carries. ``dedupe_marker`` is a 64-char indexed
#: column and the full 40-char SHA does not fit beside a slug, so the head is abbreviated
#: rather than truncated off the end — an over-long slug must never cost the marker the
#: one component that makes it per-head. Twelve hex chars is git's own long-abbreviation
#: length, well past the collision floor for one repository.
_REFUSAL_MARKER_HEAD_LEN = 12


def _refusal_marker(target: ReviewTarget) -> str:
    """The escalate-once key for a checks-contradiction refusal — one per claimed head.

    Bounded to the ``dedupe_marker`` column's own ``max_length`` read off the field, never
    a hand-copied 64, and composed so the head survives the bound: the SLUG is what gives
    way when there is not room for everything.
    """
    limit = DeferredQuestion._meta.get_field("dedupe_marker").max_length or 64  # noqa: SLF001 — Django's documented Model._meta API
    tail = f"#{target.pr_id}@{target.claim_head_sha.strip().lower()[:_REFUSAL_MARKER_HEAD_LEN]}"
    room = max(limit - len(_REFUSAL_MARKER_PREFIX) - len(tail), 0)
    return f"{_REFUSAL_MARKER_PREFIX}{target.slug.strip()[:room]}{tail}"


def _latch_checks_contradiction(target: ReviewTarget, *, task: Task, reason: str) -> None:
    """Name the cause when this head's LAST retry is spent, and page once (#4522, #4530).

    The refusal is correct and stays. What this adds is a distinction the operator could
    not otherwise make: a claim that stops at ``refused`` says the last reviewer
    contradicted its own checks report, where one that stops saturated says only that three
    attempts ran out — which is also what three crashed reviewers look like.

    Two deliberate narrowings, both from #4530:

    ONE claim, not both. ``target.armed_by`` is the table that armed THIS run; a refusal is
    run-scoped, so it may not touch the sibling claim on the same head. Walking both let a
    codex-path refusal latch a dispatch claim whose reviewer had not run and free the review
    lock it held.

    ONLY at the bound. ``mark_refused`` no-ops below :data:`MAX_DISPATCH_ATTEMPTS`, so every
    ordinary retry survives — which matters because 6 of the 9 heads that hit this refusal
    recovered at that same head. The page follows the latch rather than the refusal: below
    the bound there is nothing terminal to report, and a run holding no claim at all has
    nothing re-arming it, so neither is worth waking the owner for.

    A push mints a new head, which has no claim and no marker, and re-arms review normally. A
    push that lands MID-review is different: the verdict binds to the new head, but the spent
    claim is still the one this run was armed at, so the latch keys ``claim_head_sha``.
    """
    latched = target.armed_by.mark_refused(slug=target.slug, pr_id=target.pr_id, head_sha=target.claim_head_sha)
    if not latched:
        return
    DeferredQuestion.record(
        _refusal_question(target, reason=reason),
        task_session=task.session,
        dedupe_marker=_refusal_marker(target),
    )


def _refusal_question(target: ReviewTarget, *, reason: str) -> str:
    """The owner-facing statement of a head that spent its last retry on a refused verdict."""
    red_head = (
        "this head"
        if target.head_sha == target.claim_head_sha
        else f"{target.head_sha[:8]}, the head the branch advanced to mid-review,"
    )
    return (
        f"[review-refusal {target.slug}#{target.pr_id}@{target.claim_head_sha[:8]}] This head has used all "
        f"{MAX_DISPATCH_ATTEMPTS} auto-review attempts, and the last one returned a merge_safe verdict "
        f"over checks a LIVE workflow-run read at {red_head} confirms are RED: {reason} "
        f"Auto-review is done for this head — not because the tree is unreviewable, but because the "
        f"retries are spent. A new push re-arms review by itself. Fix the red checks and push, land a "
        f"human verdict, or close the PR?"
    )


def _returned_review_verdict(result: AgentResultBlob, *, phase: str) -> "ReviewVerdictEnvelope | None":
    """The typed verdict *result* hands back on a verdict-recording phase, else ``None``.

    *phase* is the already-normalized token, so the caller's own phase decisions and this
    one can never read the same run differently.
    """
    if phase not in _REVIEW_VERDICT_PHASES:
        return None
    raw = result.get("review_verdict")
    return cast("ReviewVerdictEnvelope", raw) if isinstance(raw, dict) else None


def _unpersisted_verdict_error(target: ReviewTarget) -> str:
    """Refuse a recording the consumers' own lookup cannot find, or ``""`` (#4308).

    The write reporting success is not the same fact as the row being readable under the
    key the merge guard and the landed-work guard query, and only a read-back distinguishes
    them. Without it a reviewing task completed exit 0 over a verdict that reached nothing —
    indistinguishable from a review that ran and approved.
    """
    if verdict_at(target) is not None:
        return ""
    return (
        f"review verdict recorded but not persisted: no verdict is readable for "
        f"{target.slug}#{target.pr_id} at the reviewed head {target.head_sha[:8]} on read-back, so "
        f"nothing downstream can see this judgement"
    )


def _require_verdict_read_back(target: ReviewTarget) -> None:
    if refusal := _unpersisted_verdict_error(target):
        raise ReviewVerdictError(refusal)
