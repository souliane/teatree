"""Record an agent result envelope back onto a ``Task`` as a ``TaskAttempt``.

The single contract for turning a structured agent result into a terminal
``Task`` outcome, shared by two callers. ``run_headless`` is the detached
``claude -p`` subprocess path (now reserved for genuinely headless, non-loop
work). ``manage.py task record-attempt`` is the in-session ``/loop`` slot path:
after the slot's ``Agent`` sub-agent returns, the slot hands the same result
envelope here so an INTERACTIVE phase task completes (and the ticket advances)
exactly as the headless path would have.

Both go through :func:`record_result_envelope`, so the schema-key check, the
phase-evidence gate (#1284), the usage stamping, and the
``complete`` / ``fail`` decision live in ONE place and cannot drift between
the two dispatch backends.
"""

import dataclasses
import json
from typing import TYPE_CHECKING, cast

from django.utils import timezone

from teatree.agents.landing_verification import landing_verification_error
from teatree.agents.outage_classifier import outage_signature
from teatree.agents.result_schema import (
    RESULT_JSON_SCHEMA,
    AgentResultBlob,
    AnswerEnvelope,
    ArticleSuggestion,
    ReviewVerdictEnvelope,
    TriageRecommendation,
    answer_text,
    check_evidence,
    recommendation_issue_url,
    suggestion_url,
)
from teatree.core.gates.critic_gate import record_returned_critic_verdict
from teatree.core.gates.directive_interpret_gate import record_returned_directive_interpretation
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import (
    DeferredQuestion,
    Finding,
    PendingArticleSuggestion,
    PendingTriageRecommendation,
    ReviewLoop,
    ReviewLoopRound,
    ReviewVerdict,
    ReviewVerdictError,
    Task,
    TaskAttempt,
    Worktree,
)
from teatree.core.models.ticket_worktree_checks import worktree_has_commits_ahead
from teatree.utils import git
from teatree.utils.run import CommandFailedError
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models import Ticket


@dataclasses.dataclass(frozen=True)
class AttemptUsage:
    """Usage stats stamped onto a recorded :class:`TaskAttempt`.

    All optional — a backend reports what it has. ``claude -p`` parses these
    from the CLI envelope; an in-session ``record-attempt`` may omit them.
    """

    agent_session_id: str = ""
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    # souliane/teatree#657: the Layer-2 lane (``TaskAttempt.Lane``) this
    # attempt's credential authenticated through, or ``""`` when unattributed.
    lane: str = ""
    # #3157 E5: whether ``cost_usd`` is a price-table ESTIMATE (True) rather than a real
    # reported (CLI/SDK/metered-router) figure. Default True so a recorder path that does
    # not compute a reported cost is flagged conservatively as an estimate.
    cost_is_estimated: bool = True


class ResultEnvelopeError(ValueError):
    """The supplied result envelope is not a JSON object."""


def parse_result_envelope(raw: str) -> AgentResultBlob:
    """Parse a JSON result object, raising :class:`ResultEnvelopeError` otherwise.

    Accepts the exact envelope shape ``run_headless`` parses out of the agent
    text: a single JSON object whose keys are the
    :data:`~teatree.agents.result_schema.RESULT_JSON_SCHEMA` fields
    (``summary``, ``files_modified``, ``needs_user_input`` …). A non-object
    payload is rejected up front so a malformed hand-off never silently
    completes a task on an empty result.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        msg = f"result is not valid JSON: {exc}"
        raise ResultEnvelopeError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "result must be a JSON object"
        raise ResultEnvelopeError(msg)
    return parsed


def validate_result_keys(result: AgentResultBlob) -> str:
    """Return an error message if *result* carries keys outside the schema.

    Only the ``additionalProperties: false`` rule is enforced (no full
    JSON-Schema dependency), mirroring the headless path's ``headless_result.validate_result``.
    """
    allowed = set(RESULT_JSON_SCHEMA.get("properties", {}).keys())  # type: ignore[union-attr]
    unexpected = set(result) - allowed
    if unexpected:
        return f"Agent result contains unexpected keys: {', '.join(sorted(unexpected))}"
    return ""


def record_result_envelope(
    task: Task,
    result: AgentResultBlob,
    *,
    phase: str = "",
    usage: AttemptUsage | None = None,
) -> TaskAttempt:
    """Record *result* as a ``TaskAttempt`` and drive the ``Task`` to terminal.

    Validation order: schema-key check → OUTAGE check (#1764) → per-phase
    evidence gate (#1284) → LANDING check (coding/debugging must have committed) —
    a failure on any records a FAILED attempt and fails the task (``exit_code=0``
    so it reads as a clean refusal, not a crash). The landing check re-reads the
    ticket worktree's git state so a coder that reported ``files_modified`` while
    nothing was committed (the yield-without-landing stall) lands FAILED with a
    ``landing_unverified`` diagnostic — which the bounded auto-requeue sweep then
    retries-if-transient / escalates, instead of the ticket FSM silently
    advancing over unlanded work. The
    outage check runs BEFORE the evidence gate so an outage death that happens
    to carry evidence (the "API error laundered as a completion" class) still
    lands FAILED with the diagnostic signature, never COMPLETED — the ticket FSM
    must not advance over work an outage interrupted. On success the attempt is
    COMPLETED and ``task.complete`` fires, auto-advancing the ticket FSM (a
    ``needs_user_input`` result completes the task too — ``_advance_ticket`` then
    schedules the interactive follow-up rather than firing the phase
    transition).
    """
    usage = usage or AttemptUsage()
    schema_error = validate_result_keys(result)
    if schema_error:
        return _record_failure(task, error=schema_error, result=result)

    signature = outage_signature(result)
    if signature:
        return _record_failure(task, error=f"outage_death: {signature}", result=result)

    evidence_error = check_evidence(result, phase or task.phase)
    if evidence_error:
        salvaged = _salvage_coding_result(task, result, phase=phase)
        if salvaged is None:
            return _record_failure(task, error=evidence_error, result=result)
        result = salvaged

    landing_error = landing_verification_error(task, phase=phase)
    if landing_error:
        return _record_failure(task, error=landing_error, result=result)

    server_side_error = _record_returned_envelopes(task, result, phase=phase)
    if server_side_error:
        return _record_failure(task, error=server_side_error, result=result)

    _maybe_record_plan_artifact(task, result, phase=phase)
    _maybe_record_article_suggestions(task, result, phase=phase)
    _maybe_record_triage_recommendations(task, result, phase=phase)
    _maybe_record_answer_draft(task, result, phase=phase)

    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=0,
        result=result,
        agent_session_id=usage.agent_session_id,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        num_turns=usage.num_turns,
        lane=usage.lane,
        cost_is_estimated=usage.cost_is_estimated,
    )
    task.complete(result_artifact_path="")
    return attempt


def _record_returned_envelopes(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    """Record every shell-denied hand-back that carries a maker≠checker write, short-circuit.

    A headless phase denied the shell RETURNS its typed verdict/sketch instead of
    writing it; the orchestrator (a different actor) records it here. Each recorder is
    a no-op unless its own dispatch/verdict is present on the task, and returns an
    error string when the returned artifact is malformed or maker-graded — the first
    such error stops the chain so the caller fails the task and the block surfaces.
    """
    review_error = _maybe_record_review_verdict(task, result, phase=phase)
    if review_error:
        return review_error
    critic_error = record_returned_critic_verdict(task, result)
    if critic_error:
        return critic_error
    return record_returned_directive_interpretation(task, result)


#: Reviewing phases whose returned ``review_verdict`` the orchestrator records
#: server-side (corr-11). These phases are denied the shell (PR-11), so their
#: reviewer hands the verdict back instead of running ``t3 <overlay> review record``.
_REVIEW_VERDICT_PHASES = frozenset({"reviewing", "e2e_reviewing"})
#: Default reviewer identity when the envelope omits one — a non-maker/loop token
#: (``ReviewVerdict.record`` refuses a maker/coding/loop identity, §17.8 clause 3).
_DEFAULT_HEADLESS_REVIEWER = "headless-reviewer"


@dataclasses.dataclass(frozen=True, slots=True)
class _ReviewTarget:
    """The PR a reviewing task's verdict binds to, resolved from the dispatch context."""

    slug: str
    pr_id: int
    head_sha: str
    ticket: "Ticket | None"


def _maybe_record_review_verdict(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    """Record a reviewing task's returned ``review_verdict`` server-side (corr-11).

    The orchestrator half of the headless review lane: a Bash-denied reviewer
    RETURNS a typed ``review_verdict``; this records the ``ReviewVerdict`` (which
    resolves the per-MR :class:`MRReviewLock`) and advances any open external
    review loop for the PR's ticket — maker≠checker holds because THIS actor is
    not the author. Returns an error string when the verdict is malformed or the
    reviewer identity is a maker/loop role (the caller fails the task so the
    block surfaces), else ``""``. A non-reviewing phase, a result without a
    ``review_verdict``, or a reviewing task with no resolvable PR target is a
    no-op (``""``).
    """
    if normalize_phase(phase or task.phase) not in _REVIEW_VERDICT_PHASES:
        return ""
    raw_envelope = result.get("review_verdict")
    if not isinstance(raw_envelope, dict):
        return ""
    target = _resolve_review_target(task)
    if target is None:
        return ""

    envelope = cast("ReviewVerdictEnvelope", raw_envelope)
    raw_findings = envelope.get("findings", [])
    findings = (
        [Finding.from_dict(item) for item in raw_findings if isinstance(item, dict)]
        if isinstance(raw_findings, list)
        else []
    )
    try:
        recorded = ReviewVerdict.record(
            pr_id=target.pr_id,
            slug=target.slug,
            reviewed_sha=str(envelope.get("reviewed_sha") or "").strip() or target.head_sha,
            verdict=str(envelope.get("verdict", "")),
            reviewer_identity=str(envelope.get("reviewer_identity") or _DEFAULT_HEADLESS_REVIEWER),
            findings=findings,
            gh_verify_result=str(envelope.get("gh_verify_result") or "green"),
            blast_class=str(envelope.get("blast_class") or "logic"),
            ticket=target.ticket,
        )
    except ReviewVerdictError as exc:
        return f"review verdict recording refused: {exc}"
    _advance_open_review_loop(recorded)
    return ""


def _resolve_review_target(task: Task) -> "_ReviewTarget | None":
    """Resolve the PR a reviewing task's verdict binds to, or ``None``.

    Two dispatch contexts carry the target: the #68 auto-review dispatch (the
    lock-holding path) links the reviewing task to an
    :class:`AutoReviewDispatch` row carrying ``(slug, pr_id, head_sha)``; an
    external :class:`ReviewLoop` reviewer leg links via a
    :class:`ReviewLoopRound`, resolving the PR from the loop ticket's latest
    :class:`PullRequest` URL. ``None`` for a reviewing task with neither — its
    returned verdict is evidence but has no PR to bind to.
    """
    dispatch = task.auto_review_dispatches.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if dispatch is not None:
        return _ReviewTarget(slug=dispatch.slug, pr_id=dispatch.pr_id, head_sha=dispatch.head_sha, ticket=task.ticket)

    slot = ReviewLoopRound.objects.filter(task=task).select_related("review_loop", "review_loop__ticket").first()
    if slot is None or slot.review_loop.variant != ReviewLoop.Variant.EXTERNAL:
        return None
    ticket = slot.review_loop.ticket
    pr = ticket.pull_requests.order_by("-pk").first()
    if pr is None:
        return None
    ref = pr_ref_from_url(pr.url)
    if ref is None:
        return None
    return _ReviewTarget(slug=ref.slug, pr_id=ref.pr_id, head_sha="", ticket=ticket)


def _advance_open_review_loop(recorded: ReviewVerdict) -> None:
    """Advance the open external :class:`ReviewLoop` for *recorded*'s ticket (#2298).

    Mirrors the ``review record`` CLI's loop-advance so a headless verdict drives
    the loop FSM identically: a merge_safe terminates at PASSED, a HOLD re-arms
    an author leg (or exhausts). Best-effort — a loop-advance failure never turns
    verdict recording into a task failure; the periodic sweep is the backstop.
    """
    if recorded.ticket_id is None:  # ty: ignore[unresolved-attribute]
        return
    loop = ReviewLoop.open_external_for_ticket(recorded.ticket_id)  # ty: ignore[unresolved-attribute]
    if loop is None:
        return
    try:
        loop.advance_from_recorded_verdict(recorded)
    except Exception:  # noqa: BLE001 — loop advance must never break verdict recording.
        return


def _maybe_record_plan_artifact(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415 — deferred: ORM/app-registry

    effective_phase = normalize_phase(phase or task.phase)
    plan_text = result.get("plan_text")
    if effective_phase != "planning" or not isinstance(plan_text, str) or not plan_text.strip():
        return
    recorded_by = (task.session.agent_id or "").strip() or "planning"
    # SELFCATCH-3: the planner envelope carries the base SHA it planned against and
    # the four-section adequacy manifest. Under require_plan_adequacy, record()
    # refuses a thin plan missing them — a planner that produced a scope-only spec
    # fails loud here rather than dispatching a coder against nothing.
    base_sha = result.get("base_sha")
    adequacy = result.get("adequacy")
    PlanArtifact.record(
        ticket=task.ticket,
        plan_text=plan_text,
        recorded_by=recorded_by,
        base_sha=base_sha if isinstance(base_sha, str) else "",
        adequacy=adequacy if isinstance(adequacy, dict) else None,
    )


#: Shell-denied reactive phases whose headless agent hands its work back through
#: a typed envelope channel (#9): the agent cannot run the ``t3`` CLI, so the
#: recorder is the server-side half that persists the returned structure. The
#: ``PHASE_REQUIRED_EVIDENCE`` gate has already refused a summary-only run before
#: these fire, so the channel field is present and non-empty here.
_SCANNING_NEWS_PHASE = "scanning_news"
_TRIAGE_ASSESSING_PHASE = "triage_assessing"
_ANSWERING_PHASE = "answering"


def _maybe_record_article_suggestions(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    """Persist a scanning_news agent's returned ``article_suggestions`` (corr-11, #9).

    One ``PENDING`` :class:`PendingArticleSuggestion` per candidate, idempotent by
    source URL (a re-scan never duplicates) and behind the same ask-gate the
    scanner used to enqueue directly — the shell-denied agent hands the batch
    back, the server persists it. A non-scanning_news phase or a result with no
    ``article_suggestions`` list is a no-op.
    """
    if normalize_phase(phase or task.phase) != _SCANNING_NEWS_PHASE:
        return
    suggestions = result.get("article_suggestions")
    if not isinstance(suggestions, list):
        return
    overlay = task.ticket.overlay
    for raw_item in suggestions:
        url = suggestion_url(raw_item)
        if not url:
            continue
        item = cast("ArticleSuggestion", raw_item)
        PendingArticleSuggestion.record_candidate(
            url=url,
            title=str(item.get("title") or ""),
            summary=str(item.get("rationale") or ""),
            overlay=overlay,
        )


def _maybe_record_triage_recommendations(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    """Persist a triage_assessing agent's returned ``triage_recommendations`` (corr-11, #9).

    One ``PENDING`` :class:`PendingTriageRecommendation` per assessed issue, idempotent
    by issue URL (a re-assessment never duplicates) and fail-closed on an unknown
    verdict — the shell-denied assessor hands the batch back and the server persists
    it. After at least one row is recorded, ONE
    :class:`DeferredQuestion` DMs the user the batch summary (correlated to the task
    via ``parked_task``, deduped per task so a resume never re-asks). **Nothing acts
    autonomously**: the interactive ``t3:triaging-issues`` skill approves/acts. A
    non-triage_assessing phase or a result with no ``triage_recommendations`` list is
    a no-op.
    """
    if normalize_phase(phase or task.phase) != _TRIAGE_ASSESSING_PHASE:
        return
    recommendations = result.get("triage_recommendations")
    if not isinstance(recommendations, list):
        return
    overlay = task.ticket.overlay
    recorded = 0
    for raw_item in recommendations:
        issue_url = recommendation_issue_url(raw_item)
        if not issue_url:
            continue
        item = cast("TriageRecommendation", raw_item)
        raw_labels = item.get("suggested_labels")
        labels = [s for s in raw_labels if isinstance(s, str)] if isinstance(raw_labels, list) else []
        row = PendingTriageRecommendation.record_candidate(
            issue_url=issue_url,
            verdict=str(item.get("verdict") or ""),
            title=str(item.get("title") or ""),
            suggested_labels=labels,
            priority=str(item.get("priority") or ""),
            duplicate_of=str(item.get("duplicate_of") or ""),
            rationale=str(item.get("rationale") or ""),
            overlay=overlay,
        )
        if row is not None:
            recorded += 1
    if recorded == 0:
        return
    DeferredQuestion.record(
        question=(
            f"Triaged {recorded} open needs-triage issue(s). Review and approve/reject each "
            f"recommendation with /t3:triaging-issues — nothing is acted on until you approve."
        ),
        session_id=task.claimed_by_session or "",
        parked_task=task,
        dedupe_marker=f"triage-batch-{task.pk}",
    )


def _maybe_record_answer_draft(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    """Route an answering agent's returned ``answer`` draft to the approval path (corr-11, #9).

    The shell-denied answerer cannot post on the user's behalf, so it hands the
    draft back: this records a :class:`DeferredQuestion` (correlated to the task
    via ``parked_task``) asking the user to approve the reply — the orchestrator
    posts on confirmation. A non-answering phase or a result with no ``answer``
    text is a no-op.
    """
    if normalize_phase(phase or task.phase) != _ANSWERING_PHASE:
        return
    raw_answer = result.get("answer")
    text = answer_text(raw_answer)
    if not text:
        return
    answer = cast("AnswerEnvelope", raw_answer)
    thread_ref = str(answer.get("thread_ref") or "").strip()
    where = f" (thread {thread_ref})" if thread_ref else ""
    DeferredQuestion.record(
        question=f"Approve this drafted reply{where}?\n\n{text}",
        session_id=task.claimed_by_session or "",
        parked_task=task,
    )


#: Phases whose landed commit can back-fill a missing ``files_modified`` envelope.
_SALVAGEABLE_PHASES = frozenset({"coding", "debugging"})


def _salvage_coding_result(task: Task, result: AgentResultBlob, *, phase: str) -> AgentResultBlob | None:
    """Return *result* with ``files_modified`` synthesized from the landed commit, or ``None``.

    The #3263 recovery: a coder committed real work but omitted the trailing
    ``files_modified`` envelope, so the evidence gate refuses and the branch is
    stranded. When the ticket worktree has a NEW commit ahead of its base AND is
    clean (``landing_verification_error`` passes — so this never salvages dirty or
    commit-less work), the committed diff's file paths ARE the evidence: synthesize
    ``files_modified`` from them so the task COMPLETES on the real landed work.
    ``None`` for a non-coding phase, or when there is nothing clean to salvage —
    the caller then records the honest evidence refusal.
    """
    if normalize_phase(phase or task.phase) not in _SALVAGEABLE_PHASES:
        return None
    if landing_verification_error(task, phase=phase):
        return None
    files = _committed_file_changes(task)
    if not files:
        return None
    salvaged = dict(result)
    salvaged["files_modified"] = files
    return salvaged


def _committed_file_changes(task: Task) -> list[dict[str, str]]:
    """``files_modified`` entries for the first ticket worktree with a commit ahead, else ``[]``."""
    for worktree in Worktree.objects.filter(ticket=task.ticket):
        if not worktree_has_commits_ahead(worktree):
            continue
        paths = _committed_paths(worktree)
        if paths:
            return [{"path": path, "action": "modified"} for path in paths]
    return []


def _committed_paths(worktree: Worktree) -> list[str]:
    # Reached only after ``worktree_has_commits_ahead`` proved a valid path + branch.
    repo_path = (worktree.extra or {}).get("worktree_path") or worktree.repo_path
    base = _base_ref(repo_path)
    try:
        out = git.run(repo=repo_path, args=["diff", "--name-only", f"{base}..{worktree.branch}"])
    except (CommandFailedError, OSError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _base_ref(repo_path: str) -> str:
    try:
        return f"origin/{git.default_branch(repo_path)}"
    except (CommandFailedError, RuntimeError):
        return "main"


def _record_failure(task: Task, *, error: str, result: AgentResultBlob | None = None) -> TaskAttempt:
    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=0,
        error=error,
        result=result or {},
    )
    task.fail()
    return attempt
