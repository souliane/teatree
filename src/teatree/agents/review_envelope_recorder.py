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

from teatree.agents.envelope_refusal import MALFORMED_RUBRIC_GRADES_PREFIX
from teatree.agents.result_schema import AgentResultBlob, ReviewVerdictEnvelope
from teatree.core.gates.rubric_gate import clear_honesty_escalation_on_pass
from teatree.core.merge.ticket_resolution import gated_ticket_for_review_task
from teatree.core.modelkit.phase_tools import ENVELOPE_VERDICT_PHASES
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import REVIEW_UNRECORDABLE_PREFIX
from teatree.core.models import (
    ChecksContradictionError,
    DeferredQuestion,
    Finding,
    ReviewVerdict,
    ReviewVerdictError,
    Rubric,
    RubricCriterion,
    RubricError,
    Task,
)
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS, AutoReviewDispatch
from teatree.core.models.review_target import ReviewTarget, review_target_for_task, verdict_at
from teatree.core.models.reviewer_identity import (
    assigned_reviewer_identity,
    is_independent_reviewer_identity,
    is_non_reviewer_role,
)
from teatree.core.review.diff_scope_probe import changed_file_set_for_findings
from teatree.core.review.head_workflow_runs import live_checks_at
from teatree.core.review.verdict_head_binding import resolve_verdict_head
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.core.models.types import RubricGrade

#: Reviewing phases whose returned ``review_verdict`` the orchestrator records
#: server-side (corr-11) — the shell-free envelope seam. Both members now ALSO
#: carry the shell (``phase_tools.VERDICT_REVIEW_PHASES``). ``reviewing`` still
#: hands the verdict back through this seam: its headless brief
#: (``phase_blocks._review_verdict_return_lines``) returns the envelope rather than
#: shelling out. ``e2e_reviewing``'s live recording path is instead the shell
#: ``t3 <overlay> review record`` from its ``/t3:e2e-review`` skill; its envelope
#: membership here is currently dormant (nothing in production returns an
#: ``e2e_reviewing`` verdict), so exactly one recording path fires per run and no
#: double-record occurs. The
#: ``codex_*`` variants are deliberately absent: no server-side envelope seam,
#: shell-only.
_REVIEW_VERDICT_PHASES = frozenset({"reviewing", "e2e_reviewing"})

#: Verdict-recording phases that also OWE rubric coverage — the ones whose brief carries
#: the checklist (``phase_blocks._reviewing_phase_lines`` injects ``rubric_brief_lines``).
#: Narrower than :data:`_REVIEW_VERDICT_PHASES` on purpose: ``e2e_reviewing`` is shown no
#: criteria, so demanding coverage there would refuse a checklist its agent never saw, and
#: briefing it instead would arm a second recording path beside its shell
#: ``t3 <overlay> review record``. That is the SAME set, for the same reason, as the phases
#: whose brief returns the envelope at all, so it IS that set rather than a second spelling
#: of it. Pinned against the brief by ``tests/teatree_agents/test_phase_blocks.py``.
_RUBRIC_GRADED_PHASES = ENVELOPE_VERDICT_PHASES


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

    A non-reviewing phase, or a result without a ``review_verdict``, is a no-op. So is a
    task answerable for NO pull request — an author-role reviewing task keyed by an issue
    URL is a self-review with no merge guard behind it, and refusing it would strand the
    author lane rather than protect anything. A task that IS answerable for one and cannot
    persist there fails loudly instead (#4308).
    """
    resolved_phase = normalize_phase(phase or task.phase)
    envelope = _returned_review_verdict(result, phase=resolved_phase)
    if envelope is None:
        return ""
    target = review_target_for_task(task)
    if target is None:
        return ""
    if not target.head_sha:
        return (
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
            _supersede_moved_head(target)
        return binding.error
    dispatch_head = target.head_sha
    target = dataclasses.replace(target, head_sha=binding.head)

    ticket = gated_ticket_for_review_task(task) if resolved_phase in _RUBRIC_GRADED_PHASES else None
    rubric = Rubric.objects.active_for_ticket(ticket) if ticket is not None else None
    returned = envelope.get("rubric_grades")
    try:
        # Scoped to a rubric that EXISTS, like both refusals below — with none, nothing is stamped.
        grades: list[RubricGrade] = [] if rubric is None else Rubric.normalize_grades(returned)
    except RubricError as exc:
        return f"{MALFORMED_RUBRIC_GRADES_PREFIX}{exc}"
    return (
        _rubric_coverage_error(rubric, grades, returned=returned)
        or _merge_safe_over_fail_error(rubric, envelope, grades)
        or _record_verdict_and_grades(task, envelope, target=target, rubric=rubric, grades=grades)
        or _settle_recorded_verdict(task, target, rubric=rubric, dispatch_head=dispatch_head)
    )


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


def _rubric_coverage_error(rubric: "Rubric | None", grades: "list[RubricGrade]", *, returned: object) -> str:
    """Refuse a verdict that leaves any criterion of *rubric* ungraded, or ``""``.

    ORDERING IS LOAD-BEARING, which is why this runs BEFORE any write.
    ``ReviewVerdict.record`` retires the per-head claim and releases the review lock in
    the same transaction that records the verdict — so a verdict written over a
    half-graded rubric leaves nothing to re-arm review, while the done-gate goes on
    refusing the merge for the PENDING criterion. The head is then unmergeable forever.

    A ticket with no rubric, and a PR no ticket owns, owe no grades at all: that is
    byte-for-byte the subject ``core.merge.ticket_gates`` already skips, and widening it
    here would refuse a colleague's PR the merge gate never grades.
    """
    if rubric is None:
        return ""
    ungraded = rubric.ungraded_ordinals(grades)
    if not ungraded:
        return ""
    named = ", ".join(f"#{ordinal}" for ordinal in ungraded)
    return (
        f"{MALFORMED_RUBRIC_GRADES_PREFIX}criteria {named} of ticket {rubric.ticket.pk} are ungraded "
        f"({_returned_grades_phrase(returned)}) — "
        f"a verdict that leaves a criterion PENDING records nothing, because recording it would retire "
        f"this head's review claim while the done-gate still refuses the merge, leaving the head "
        f"unmergeable with no reviewer left to re-arm. You are the independent verifier: grade EVERY "
        f"criterion and return the verdict again, as "
        f'"rubric_grades": [{{"ordinal": <int>, "status": "pass"|"fail", "rationale": "<what proves it>"}}] '
        f"— a PASS cites the test that proves it, a FAIL needs no citation"
    )


def _returned_grades_phrase(returned: object) -> str:
    """What the envelope actually carried under ``rubric_grades`` — the actionable half.

    Naming only the criteria leaves a reviewer that returned ``{"0": "pass"}`` reading a
    refusal about coverage when its payload was never a list of grades at all.
    """
    if returned is None:
        return 'you returned no "rubric_grades" at all'
    if isinstance(returned, list):
        return f'your "rubric_grades" carried {len(returned)} grade(s)'
    return f'your "rubric_grades" was a {type(returned).__name__}, not a JSON array'


def _merge_safe_over_fail_error(
    rubric: "Rubric | None", envelope: "ReviewVerdictEnvelope", grades: "list[RubricGrade]"
) -> str:
    """Refuse a ``merge_safe`` verdict carrying a criterion the same envelope grades FAIL.

    The sibling of ``ReviewVerdict._assert_checks_admit_merge_safe`` one field over: that
    one refuses merge_safe over checks the reviewer itself reported RED, this one over an
    acceptance criterion the reviewer itself grades unmet. Both are a contradiction between
    two fields ONE reviewer wrote in ONE envelope, and neither is a statement about the tree.

    Recorded, it retires the per-head claim and releases the lock, and the done-gate then
    refuses the merge on a FAIL no bypass overrides — recoverable only by a push that mints
    a new head. Refusing before the write keeps the head re-reviewable instead.

    Scoped to a rubric that EXISTS, like the coverage refusal beside it: with no rubric the
    grade is stamped nowhere, so there is no FAIL for the done-gate to refuse the merge on.
    """
    if rubric is None or str(envelope.get("verdict", "")).strip().lower() != ReviewVerdict.Verdict.MERGE_SAFE:
        return ""
    failed = [grade["ordinal"] for grade in grades if grade["status"].strip().lower() == RubricCriterion.Status.FAIL]
    if not failed:
        return ""
    named = ", ".join(f"#{ordinal}" for ordinal in failed)
    return (
        f"{MALFORMED_RUBRIC_GRADES_PREFIX}your verdict is merge_safe but you graded criteria {named} FAIL — "
        f"one envelope cannot both vouch for the head and record an unmet acceptance criterion, and a FAIL is "
        f"never overridable, so recording this would retire the review claim onto a merge the done-gate refuses "
        f"forever. Return `hold` if the criteria really are unmet, or re-grade them honestly if they are not"
    )


def _record_verdict_and_grades(
    task: Task,
    envelope: "ReviewVerdictEnvelope",
    *,
    target: ReviewTarget,
    rubric: "Rubric | None",
    grades: "list[RubricGrade]",
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
    raw_findings = envelope.get("findings", [])
    findings = (
        [Finding.from_dict(item) for item in raw_findings if isinstance(item, dict)]
        if isinstance(raw_findings, list)
        else []
    )
    identity = _recorded_reviewer_identity(target, envelope)
    try:
        with transaction.atomic():
            ReviewVerdict.record(
                pr_id=target.pr_id,
                slug=target.slug,
                reviewed_sha=target.head_sha,
                verdict=str(envelope.get("verdict", "")),
                reviewer_identity=identity,
                findings=findings,
                gh_verify_result=str(envelope.get("gh_verify_result") or "green"),
                blast_class=str(envelope.get("blast_class") or "logic"),
                ticket=task.ticket,
                lock_holder=target.lock_holder,
                changed_files=changed_file_set_for_findings(findings, slug=target.slug, pr_id=target.pr_id),
                merge_result_retake=bool(envelope.get("merge_result_retake")),
                live_checks=live_checks_at,
            )
            if rubric is not None:
                rubric.apply_grades(grades, grader_identity=identity, reviewed_sha=target.head_sha)
    except ReviewVerdictError as exc:
        # The one refusal class a re-dispatch can never satisfy at this head is latched;
        # every other one names something the next reviewer could get right, so it keeps
        # the ordinary retry. The discrimination is the EXCEPTION TYPE — the raise site
        # itself — never the message text a reword would detach this from.
        if isinstance(exc, ChecksContradictionError):
            _latch_checks_contradiction(target, task=task, reason=str(exc))
        return f"review verdict recording refused: {exc}"
    except RubricError as exc:
        return f"{MALFORMED_RUBRIC_GRADES_PREFIX}{exc}"
    return ""


def _settle_recorded_verdict(task: Task, target: ReviewTarget, *, rubric: "Rubric | None", dispatch_head: str) -> str:
    """After a recorded verdict: rebind the claim, clear a satisfied escalation, and prove the row reads back."""
    _rebind_claim_to_recorded_head(task, target, dispatch_head=dispatch_head)
    if rubric is not None and rubric.is_fully_passed_at(target.head_sha):
        clear_honesty_escalation_on_pass(rubric.ticket)
    return _unpersisted_verdict_error(target)


def _supersede_moved_head(target: ReviewTarget) -> None:
    """Retire the claim for a head the PR has advanced past, so review re-arms at the new one.

    Scoped to the #68 dispatch ledger on purpose: it is the only per-head claim the PR sweep
    re-arms, so superseding a codex marker would release a review lock nothing re-takes.
    """
    if target.armed_by is not AutoReviewDispatch:
        return
    AutoReviewDispatch.mark_superseded(slug=target.slug, pr_id=target.pr_id, head_sha=target.head_sha)


def _rebind_claim_to_recorded_head(task: Task, target: ReviewTarget, *, dispatch_head: str) -> None:
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
    if target.head_sha == dispatch_head:
        return
    if target.armed_by is AutoReviewDispatch:
        AutoReviewDispatch.mark_recorded_at(
            slug=target.slug,
            pr_id=target.pr_id,
            head_sha=dispatch_head,
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
    """The escalate-once key for a checks-contradiction refusal — one per reviewed head.

    Bounded to the ``dedupe_marker`` column's own ``max_length`` read off the field, never
    a hand-copied 64, and composed so the head survives the bound: the SLUG is what gives
    way when there is not room for everything.
    """
    limit = DeferredQuestion._meta.get_field("dedupe_marker").max_length or 64  # noqa: SLF001 — Django's documented Model._meta API
    tail = f"#{target.pr_id}@{target.head_sha.strip().lower()[:_REFUSAL_MARKER_HEAD_LEN]}"
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

    A push mints a new head, which has no claim and no marker, and re-arms review normally.
    """
    latched = target.armed_by.mark_refused(slug=target.slug, pr_id=target.pr_id, head_sha=target.head_sha)
    if not latched:
        return
    DeferredQuestion.record(
        _refusal_question(target, reason=reason),
        session_id=str(task.session_id or ""),  # ty: ignore[unresolved-attribute]
        dedupe_marker=_refusal_marker(target),
    )


def _refusal_question(target: ReviewTarget, *, reason: str) -> str:
    """The owner-facing statement of a head that spent its last retry on a refused verdict."""
    return (
        f"[review-refusal {target.slug}#{target.pr_id}@{target.head_sha[:8]}] This head has used all "
        f"{MAX_DISPATCH_ATTEMPTS} auto-review attempts, and the last one returned a merge_safe verdict "
        f"over checks a LIVE workflow-run read at this head confirms are RED: {reason} "
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
