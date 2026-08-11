"""Record an agent result envelope back onto a ``Task`` as a ``TaskAttempt``.

The single contract for turning a structured agent result into a terminal
``Task`` outcome, shared by two callers. ``run_agent`` is the detached
``claude -p`` subprocess path (now reserved for genuinely headless, non-loop
work). ``manage.py task record-attempt`` is the in-session ``/loop`` slot path:
after the slot's ``Agent`` sub-agent returns, the slot hands the same result
envelope here so an INTERACTIVE phase task completes (and the ticket advances)
exactly as the agent path would have.

Both go through :func:`record_result_envelope`, so the schema-key check, the
phase-evidence gate (#1284), the usage stamping, and the
``complete`` / ``fail`` decision live in ONE place and cannot drift between
the two dispatch backends.
"""

import dataclasses
import json
from typing import TYPE_CHECKING, cast

from django.utils import timezone

from teatree.agents.action_verification import action_verification_error
from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR
from teatree.agents.landing_verification import commits_ahead_or_unknown, landing_verification_error
from teatree.agents.outage_classifier import outage_signature
from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, AgentResultBlob, ReviewVerdictEnvelope, check_evidence
from teatree.core.gates.critic_gate import record_returned_critic_verdict
from teatree.core.gates.directive_interpret_gate import record_returned_directive_interpretation
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Finding, ReviewVerdict, ReviewVerdictError, Task, TaskAttempt, Worktree
from teatree.core.models.auto_review_dispatch import LOOP_SCANNER_HOLDER
from teatree.utils import git
from teatree.utils.run import CommandFailedError

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
    # #3673 Tier 3 dispatch provenance: the per-tier reasoning effort the spawn
    # resolved and the resolved skill-bundle names. Both empty on a recorder path
    # that has no dispatch context (e.g. an in-session record-attempt).
    reasoning_effort: str = ""
    skills_loaded: list[str] = dataclasses.field(default_factory=list)
    # Tool calls the run emitted. ``None`` means UNMEASURED — the in-session
    # ``record-attempt`` path hands over a sub-agent's envelope and never saw its
    # tool stream — and is deliberately distinct from a measured ``0``, which is
    # the positive evidence that the run could not act
    # (:mod:`teatree.agents.action_verification`).
    tool_calls: int | None = None


class ResultEnvelopeError(ValueError):
    """The supplied result envelope is not a JSON object."""


def parse_result_envelope(raw: str) -> AgentResultBlob:
    """Parse a JSON result object, raising :class:`ResultEnvelopeError` otherwise.

    Accepts the exact envelope shape ``run_agent`` parses out of the agent
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

    The single validation seam for every agent result — the headless driver and
    ``record-attempt`` both land here. Only the ``additionalProperties: false``
    rule is enforced (no full JSON-Schema dependency).
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
    envelope_parsed: bool = True,
) -> TaskAttempt:
    """Record *result* as a ``TaskAttempt`` and drive the ``Task`` to terminal.

    Validation order: schema-key check → OUTAGE check (#1764) → ACTION check
    (an acting phase must have touched a tool) → per-phase evidence gate (#1284) →
    LANDING check (coding/debugging must have committed) —
    a failure on any records a FAILED attempt and fails the task (``exit_code=0``
    so it reads as a clean refusal, not a crash). The action check runs BEFORE the
    evidence gate so a toolless run never reaches the coding salvage below: on a
    long-lived branch the salvage would otherwise synthesize ``files_modified``
    from the whole branch diff and complete a run that never acted
    (:mod:`teatree.agents.action_verification`). The landing check re-reads the
    ticket worktree's git state so a coder that reported ``files_modified`` while
    nothing was committed (the yield-without-landing stall) lands FAILED with a
    ``landing_unverified`` diagnostic — which the bounded auto-requeue sweep then
    retries-if-transient / escalates, instead of the ticket FSM silently
    advancing over unlanded work. The
    outage check runs BEFORE the evidence gate so an outage death that happens
    to carry evidence (the "API error laundered as a completion" class) still
    lands FAILED with the diagnostic signature, never COMPLETED — the ticket FSM
    must not advance over work an outage interrupted.

    *envelope_parsed* is how the RUNNER tells this recorder whether the blob it
    hands over was actually parsed out of the agent's output. It is ``False``
    only for the manufactured ``{"summary": <prose>}`` of a run that emitted no
    JSON at all: the evidence gate then refuses "you omitted `tests_run`", which
    reads as an envelope with a missing key and is not what happened. On an
    evidence phase that refusal is therefore re-diagnosed as
    :data:`~teatree.agents.envelope_refusal.NO_ENVELOPE_ERROR`. The salvage still
    runs first, so a coder that landed real work is still rescued; the in-session
    ``record-attempt`` path parsed its own envelope and keeps the per-field
    diagnosis by default. On success the attempt is
    COMPLETED and ``task.complete`` fires, auto-advancing the ticket FSM (a
    ``needs_user_input`` result completes the task too — ``_advance_ticket`` then
    schedules the interactive follow-up rather than firing the phase
    transition).
    """
    usage = usage or AttemptUsage()
    checked = _check_before_recording(task, result, phase=phase, usage=usage, envelope_parsed=envelope_parsed)
    result = checked.result
    if checked.error:
        return _record_failure(task, error=checked.error, result=result)

    server_side_error = _record_returned_envelopes(task, result, phase=phase)
    if server_side_error:
        return _record_failure(task, error=server_side_error, result=result)

    _maybe_record_plan_artifact(task, result, phase=phase)
    record_reactive_envelopes(task, result, phase=phase)

    attempt = TaskAttempt.objects.create(
        task=task,
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
        reasoning_effort=usage.reasoning_effort,
        skills_loaded=list(usage.skills_loaded),
    )
    task.complete(result_artifact_path="")
    return attempt


@dataclasses.dataclass(frozen=True, slots=True)
class _PreRecordCheck:
    """The first refusal of the side-effect-free validation chain, and the result to record.

    ``result`` is the possibly-SALVAGED envelope: the #3263 coding salvage replaces a
    missing ``files_modified`` with the landed commit's paths, and the caller must
    record that envelope rather than the one it passed in.
    """

    error: str
    result: AgentResultBlob


def _check_before_recording(
    task: Task,
    result: AgentResultBlob,
    *,
    phase: str,
    usage: AttemptUsage,
    envelope_parsed: bool,
) -> _PreRecordCheck:
    """Run the ordered refusal chain that precedes any recording side effect.

    Order is load-bearing and documented on :func:`record_result_envelope`. Split
    out so that function stays a short record-or-refuse decision over ONE verdict
    rather than a ladder of early returns.
    """
    schema_error = validate_result_keys(result)
    if schema_error:
        return _PreRecordCheck(schema_error, result)

    signature = outage_signature(result)
    if signature:
        return _PreRecordCheck(f"outage_death: {signature}", result)

    action_error = action_verification_error(phase or task.phase, tool_calls=usage.tool_calls)
    if action_error:
        return _PreRecordCheck(action_error, result)

    evidence_error = check_evidence(result, phase or task.phase)
    if evidence_error:
        salvaged = _salvage_coding_result(task, result, phase=phase)
        if salvaged is None:
            return _PreRecordCheck(evidence_error if envelope_parsed else NO_ENVELOPE_ERROR, result)
        result = salvaged

    return _PreRecordCheck(landing_verification_error(task, phase=phase), result)


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
#: server-side (corr-11) — the shell-free envelope seam. Both members now ALSO
#: carry the shell (``phase_tools.VERDICT_REVIEW_PHASES``). ``reviewing`` still
#: hands the verdict back through this seam: its headless brief
#: (``prompt._REVIEW_VERDICT_RETURN_LINES``) returns the envelope rather than
#: shelling out. ``e2e_reviewing``'s live recording path is instead the shell
#: ``t3 <overlay> review record`` from its ``/t3:e2e-review`` skill; its envelope
#: membership here is currently dormant (nothing in production returns an
#: ``e2e_reviewing`` verdict), so exactly one recording path fires per run and no
#: double-record occurs. The
#: ``codex_*`` variants are deliberately absent: no server-side envelope seam,
#: shell-only.
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
    #: Identity under which THIS dispatch path holds the per-MR review lock, or
    #: "" for a path that took none and cannot know whose identity did. A named
    #: non-holder releases nothing; an unnamed one releases (MRReviewLock.resolve).
    lock_holder: str = ""


def _maybe_record_review_verdict(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    """Record a reviewing task's returned ``review_verdict`` server-side (corr-11).

    The orchestrator half of the headless review lane: a Bash-denied reviewer
    RETURNS a typed ``review_verdict``; this records the ``ReviewVerdict`` (which
    resolves the per-MR :class:`MRReviewLock`) — maker≠checker holds because THIS
    actor is not the author. Returns an error string when the verdict is malformed, the
    reviewer identity is a maker/loop role, or the reviewer's self-asserted head diverges
    from the dispatch head (the caller fails the task so the block surfaces), else ``""``.
    A non-reviewing phase, a result without a ``review_verdict``, or a reviewing task with
    no resolvable PR target is a no-op (``""``).
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
    binding_error = _head_binding_error(
        asserted=str(envelope.get("reviewed_sha") or "").strip(), dispatch_head=target.head_sha
    )
    if binding_error:
        return binding_error
    raw_findings = envelope.get("findings", [])
    findings = (
        [Finding.from_dict(item) for item in raw_findings if isinstance(item, dict)]
        if isinstance(raw_findings, list)
        else []
    )
    try:
        ReviewVerdict.record(
            pr_id=target.pr_id,
            slug=target.slug,
            reviewed_sha=target.head_sha,
            verdict=str(envelope.get("verdict", "")),
            reviewer_identity=str(envelope.get("reviewer_identity") or _DEFAULT_HEADLESS_REVIEWER),
            findings=findings,
            gh_verify_result=str(envelope.get("gh_verify_result") or "green"),
            blast_class=str(envelope.get("blast_class") or "logic"),
            ticket=target.ticket,
            lock_holder=target.lock_holder,
        )
    except ReviewVerdictError as exc:
        return f"review verdict recording refused: {exc}"
    return ""


#: Shortest self-asserted prefix that still identifies the dispatch head — git's own
#: abbreviation floor. Anything shorter is read as a divergence, not an abbreviation.
_MIN_ABBREVIATED_SHA_LEN = 7


def _head_binding_error(*, asserted: str, dispatch_head: str) -> str:
    """Refuse a verdict that does not bind to the dispatched tree, or ``""`` (#4126, #4168).

    The verdict is recorded at the DISPATCH head because that is the key the landed-work
    guard (:func:`~teatree.core.models.phase_landing.phase_landing_evidence`) reads: a
    verdict written at the reviewer's own ``reviewed_sha`` is unreachable there, so the
    reviewing row stays ``failed`` and is re-dispatched forever. Recording a divergent
    self-assertion at the dispatch head anyway would be worse — it would vouch for a tree
    nobody reviewed — so the divergence is surfaced instead, and a reviewer that judged a
    different tree than it was dispatched for becomes a finding rather than a silent miss.
    An abbreviated head that prefixes the dispatch head asserts the same tree.

    An OMITTED head is refused on the same reasoning (#4168): treating it as agreement
    enforced the rule only against reviewers that disclose a head, so a reviewer that said
    nothing got ``merge_safe`` recorded at the dispatch head with no check performed at all.
    The shell sibling (``t3 <overlay> review record``) already refuses an empty ``--reviewed-sha``, and
    ``build_review_contract`` hands the reviewer the literal 40-char head, so disclosing it
    costs a compliant reviewer nothing.
    """
    claimed = asserted.lower()
    head = dispatch_head.strip().lower()
    if not claimed:
        return (
            "review verdict omits reviewed_sha — the head it bound to is undisclosed, so nothing "
            f"was checked against the head this review was dispatched for ({dispatch_head}); the "
            "verdict is not recorded. Return that full 40-char head, which your brief named"
        )
    if len(claimed) >= _MIN_ABBREVIATED_SHA_LEN and head.startswith(claimed):
        return ""
    return (
        f"review verdict reviewed_sha {asserted!r} is not the head this review was dispatched for "
        f"({dispatch_head}) — a reviewer that judged a different tree than the one it was "
        f"dispatched for is itself a finding; the verdict is not recorded"
    )


def _resolve_review_target(task: Task) -> "_ReviewTarget | None":
    """Resolve the PR a reviewing task's verdict binds to, or ``None``.

    One dispatch context carries the target: the #68 auto-review dispatch (the
    lock-holding path) links the reviewing task to an
    :class:`AutoReviewDispatch` row carrying ``(slug, pr_id, head_sha)``.
    ``None`` for a reviewing task without one — its returned verdict is evidence
    but has no PR to bind to.
    """
    dispatch = task.auto_review_dispatches.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if dispatch is None:
        return None
    return _ReviewTarget(
        slug=dispatch.slug,
        pr_id=dispatch.pr_id,
        head_sha=dispatch.head_sha,
        ticket=task.ticket,
        lock_holder=LOOP_SCANNER_HOLDER,
    )


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
    """``files_modified`` entries for the first ticket worktree with a commit ahead, else ``[]``.

    A worktree whose probe cannot answer is one nothing can be salvaged FROM, so
    it is skipped like a commit-less one — never allowed to abort the scan before
    it reaches the sibling that did land the work.
    """
    for worktree in Worktree.objects.for_ticket(task.ticket):
        if commits_ahead_or_unknown(worktree) is not True:
            continue
        paths = _committed_paths(worktree)
        if paths:
            return [{"path": path, "action": "modified"} for path in paths]
    return []


def _committed_paths(worktree: Worktree) -> list[str]:
    # Reached only after ``commits_ahead_or_unknown`` proved a valid path + branch.
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
        ended_at=timezone.now(),
        exit_code=0,
        error=error,
        result=result or {},
    )
    task.fail(reason=error)
    return attempt
