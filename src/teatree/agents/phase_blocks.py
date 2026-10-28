"""The per-phase trailing blocks of the agent system context.

``prompt.build_system_context`` assembles the append; this module owns the block
it appends LAST — the directive set for the dispatched phase. Reviewing, answering,
planning and scanning_news must each RETURN their evidence in the result envelope
rather than write it through a CLI — some because the phase has no shell, reviewing
because maker≠checker reserves the write for a different actor — and the phase
evidence gate refuses a run whose envelope omits it, so each block carries the
envelope directive that keeps the run from being wasted.
"""

import json
import logging
from collections.abc import Callable

from teatree.agents.coding_prompt import _TEST_SELECTION_LINES, _coding_phase_directive, _testing_phase_lines
from teatree.agents.dispatch_preflight import (
    declared_seams_brief_lines,
    head_state_brief_lines,
    review_diff_brief_lines,
    round_brief_lines,
    rubric_brief_lines,
    standing_hold_brief_lines,
)
from teatree.agents.envelope_contract import (
    FIX_RECORD_NO_RED_FIELD_LINES,
    FIX_RECORD_RETURN_LINES,
    FIX_RECORD_REVIEW_LINES,
    FIX_RECORD_REVIEW_NO_RED_LINES,
)
from teatree.agents.read_only_draft_blocks import REQUESTING_REVIEW_LINES, SHIPPING_LINES
from teatree.agents.skill_injection import _explicit_load_name
from teatree.core.answering.work_intent import owes_work_item
from teatree.core.gates.fix_dod_gate import is_fix
from teatree.core.gates.repro_gate import failing_run_required
from teatree.core.merge.ticket_resolution import gated_ticket_for_review_task
from teatree.core.modelkit.phase_tools import ENVELOPE_VERDICT_PHASES
from teatree.core.modelkit.phases import normalize_phase, resolve_fanout_directive
from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE
from teatree.core.models import Task
from teatree.core.models.review_target import assigned_reviewer_identity_for
from teatree.core.models.reviewer_identity import REVIEWER_IDENTITY_INSTRUCTION
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.core.review.live_head import dispatch_review_target, live_head_at
from teatree.core.review.review_plan import plan_for_task

logger = logging.getLogger(__name__)

# The anti-rubber-stamp contract for a verification brief — prove the change out
# first, then grade every quality dimension.
_VERIFICATION_BRIEF_LINES: tuple[str, ...] = (
    "",
    "VERIFICATION RIGOR:",
    "1. Read the proof first, then reproduce the claimed outcome; summaries and unverified findings are not evidence.",
    "2. Grade correctness, robustness, maintainability, coherence, reliability, and proactivity separately.",
)

#: Appended when the dispatch named the identity: what the agent must do with it, and the one
#: spelling the orchestrator will still refuse rather than replace.
_ASSIGNED_IDENTITY_LINES: tuple[str, ...] = (
    "Return the ASSIGNED `reviewer_identity` verbatim; omission or respelling is replaced.",
    "A maker/coding/loop identity is refused and records nothing.",
)


# The reviewer returns its verdict in the result envelope rather than writing the
# row itself: maker≠checker requires a different actor, so the orchestrator records
# it server-side. The rubric grades ride the SAME envelope for a second reason —
# prose that must be remembered across a long review is the failure mode, and one
# envelope cannot record a verdict while forgetting the grades.
def _review_verdict_return_lines(identity: str, *, read_only_venue: bool = False) -> tuple[str, ...]:
    """The verdict-return directive; *identity* is the assigned one, ``""`` to self-name.

    An ASSIGNED identity is a literal to copy, so the agent chooses nothing and the value
    the verdict lands under is the dispatch's (#2663); without one — a review answerable for
    no pull request — the agent must satisfy the gate's vocabulary itself.
    """
    return (
        "",
        "RECORD YOUR VERDICT BY RETURNING IT in final JSON; maker≠checker: do NOT try `t3 <overlay> review record`.",
        "The orchestrator records it and releases the review lock:",
        (
            '  "review_verdict": {"verdict": "merge_safe"|"hold"'
            + ('|"unverified"' if identity else "")
            + ', "reviewed_sha": "<full 40-char HEAD SHA>",'
        ),
        f'                     "reviewer_identity": "{identity or REVIEWER_IDENTITY_INSTRUCTION}",',
        '                     "gh_verify_result": "green"|"pending"|"failed",',
        '                     "blast_class": "substrate"|"logic"|"docs",',
        '                     "findings": [{"severity": "...", "kind": "blocking"|"nit"|"note",',
        '                                   "basis": "executed"|"documentation"|"venue_limit",',
        '                                   "summary": "...", "file": "...", "line": 0,',
        '                                   "citation": "...", "reproduction": "<run and result, if executed>"}]}',
        "BLOCKING: wrong behaviour, false claim, untested behaviour, vacuous test or redundant test,",
        "duplicate definition, or red check. NIT: wording, naming, style, placement.",
        "Only BLOCKING may hold; NITs need merge_safe and follow-up.",
        ENVELOPE_FINDINGS_RULE,
        *(
            (
                "Use unverified with evidence_gaps (ci_unreadable, tests_unavailable, plan_unavailable) "
                + "for unreadable evidence; it cannot clear a merge and re-arms review.",
            )
            if identity
            else ()
        ),
        "Failed check or blocking finding means hold; PASS/LGTM/approve refused.",
        (
            (
                "Use the dispatched head in the DIFF UNDER REVIEW block as the full 40-char `reviewed_sha`."
                if identity
                else "Use `git rev-parse HEAD` in the mounted checkout as the full 40-char `reviewed_sha`."
            )
            if read_only_venue
            else ("`reviewed_sha` MUST be the full 40-char SHA you reviewed (`git rev-parse HEAD`):")
        ),
        "A verdict for another head is REFUSED; on checkout mismatch return",
        '  "review_head_mismatch": {"observed_sha": "<40-char SHA>", "reason": "<why>"}',
        "without review_verdict or owner question. Factory retries once at the live head; a repeat records a card.",
        'If CI, tests, or the plan cannot be read here, return "review_venue_limit":',
        '  {"limits": ["<limit>"]} without review_verdict; the factory retries elsewhere.',
        "HOLD needs observed code/check blocker, not a venue limit.",
        "Missing `reviewed_sha` is REFUSED.",
        "Out-of-diff blocking findings are refused without recording.",
        "Branch-only probes may blame main; recheck on the MERGE RESULT.",
        *(
            ("If the merge result cannot be built here, report review_venue_limit.",)
            if read_only_venue
            else (
                "Use `t3 review merge-tree` (plain directory, never a git worktree),",
                'then add "merge_result_retake": true only if it survives.',
            )
        ),
        *(
            (
                (
                    "Set gh_verify_result green only when WORKER CI says green at the dispatched head; "
                    "a confirmed failure requires hold; an unreadable check requires unverified "
                    "if review concluded, or review_venue_limit if it could not."
                    if identity
                    else (
                        "Pre-PR self-review may return merge_safe with gh_verify_result pending; "
                        "never claim green without worker CI."
                    )
                ),
            )
            if read_only_venue
            else ()
        ),
        "No review_verdict fails unless review_venue_limit or review_head_mismatch is returned.",
        *(_ASSIGNED_IDENTITY_LINES if identity else ()),
        "",
        "RUBRIC: grade here; independent verifier != maker:",
        (
            '  "rubric_grades": [{"ordinal": 0, "text_hash": "<hash from brief>", '
            '"status": "pass"|"fail", "rationale": "<what proves it>"}, ...]'
        ),
        "Grade every row except CARRIED; grade to override a carry.",
        "An ungraded RE-GRADE row records nothing. A PASS cites a test; a FAIL needs none.",
        *(("An unverified verdict may leave criteria ungraded; it never authorizes a merge.",) if identity else ()),
        "FAIL is a finding; no bypass overrides it. do NOT run `ticket rubric-grade`.",
    )


# Injected into an answering brief: the answering phase is denied the
# shell (agents/answerer.md tools = Read/Grep/Glob only), so it CANNOT post the
# reply itself via the Replier / `t3 <overlay> notify` CLI. It RETURNS the draft
# in the result envelope instead; the orchestrator (``attempt_recorder`` →
# ``_maybe_record_answer_draft``) posts your draft directly to the owner
# in-thread — answering the owner is not a post on the owner's behalf, so it
# is never approval-gated or deferred (an on-behalf reply to a third party
# still routes through the DeferredQuestion approval path).
# Symmetric to ``_review_verdict_return_lines`` — without this directive the
# shell-denied answerer returns a prose summary with no ``answer`` field and the
# phase evidence gate refuses ("missing required evidence for phase 'answering'").
_ANSWER_RETURN_LINES: tuple[str, ...] = (
    "",
    "RETURN YOUR REPLY AS A DRAFT (this phase has no shell — do NOT try to post via",
    "`t3 <overlay> notify`, the Replier, or any CLI; you cannot post, you HAND BACK the draft):",
    "add an `answer` object to your final JSON result. The orchestrator posts your draft",
    "directly to the owner, in-thread, on your behalf:",
    '  "answer": {"text": "<the drafted reply, in the user\'s voice — no AI signature>",',
    '             "thread_ref": "<the inbound thread ts/ref this reply targets, or \'\' if none>"}',
    "The `text` MUST be non-empty — a summary-only result with no `answer` drops the reply and",
    "the phase is refused. If you cannot answer (missing context, a decision only the user can",
    "make), draft a clarifying-question reply as the `answer` text rather than returning nothing.",
)

# Injected into an answering brief whose dispatching message implied work (#4527):
# the reply alone leaves the request nowhere intake can find it, so the phase also
# has to say what the request BECOMES. The agent cannot file it — the recorder does,
# from this channel.
_WORK_ITEM_RETURN_LINES: tuple[str, ...] = (
    "",
    "THIS REQUEST IMPLIES WORK, so ALSO add a `work_item` object to your final JSON result.",
    "Search the open backlog FIRST and prefer extending an existing issue over filing a near-duplicate:",
    '  "work_item": {"existing_issue_url": "<the backlog issue this belongs to>"}',
    "or, when nothing fits:",
    '  "work_item": {"title": "<imperative one-line title>", "body": "<what to do and why>"}',
    "or, when the reply genuinely settles it and nothing needs building:",
    '  "work_item": {"no_work_reason": "<why no work is needed>"}',
    "The body must PARAPHRASE — never paste the owner's message verbatim; this files onto a",
    "PUBLIC repo. A result with no `work_item` is refused and the task is re-dispatched.",
)

# Injected into a planning brief (#3584): the phase evidence gate
# (``PHASE_REQUIRED_EVIDENCE["planning"]``) refuses a run whose result envelope
# omits ``plan_text``, so the planner MUST place the full plan under that key —
# not only as prose or a PlanArtifact. Without this reinforcing directive the
# planner produced a plan but returned a summary-only envelope, and the attempt
# was refused for "missing required evidence for phase 'planning'" then re-run.
# Symmetric to ``_review_verdict_return_lines`` / ``_ANSWER_RETURN_LINES``.
_PLAN_RETURN_LINES: tuple[str, ...] = (
    "",
    "RETURN YOUR PLAN IN THE ENVELOPE (the phase evidence gate refuses a run with no `plan_text`):",
    "your final JSON result MUST carry the full implementation plan under the `plan_text` key:",
    '  "plan_text": "<the complete plan — file-level changes, data model, API contracts, test',
    '                strategy, and the E2E test plan / Acceptance scenarios section when UI-visible>"',
    "A summary-only result with no `plan_text` drops the plan and the phase is refused, wasting the run.",
    "ALSO return `base_sha` and `adequacy` — PlanArtifact.record REFUSES a plan without",
    "them, and the agent lane can only supply them through this envelope:",
    '  "base_sha": "<`git rev-parse origin/<target-branch>` — the full 40-char hex HEAD you planned against>",',
    '  "adequacy": {"design": {"content": "<the approach>"},',
    '               "integration_seams": {"content": ["<registry/contract/sibling path the change touches>"]},',
    '               "edge_cases": {"content": ["<edge case>"]},',
    '               "test_strategy": {"content": "<what proves it, fail-before included>"},',
    '               "acceptance_criteria": {"content": ["<one checkable criterion>"]}}',
    "Every section must be substantive OR carry an explicit reasoned negative instead of `content`",
    '(e.g. {"none_reason": "no seams: single leaf module, no registry touched"}); silence never passes.',
    "`acceptance_criteria` BECOMES the ticket's rubric, which an independent verifier grades before",
    "the ticket can merge or be delivered — so write criteria that can FAIL, never ones satisfied by",
    "inaction, and that can PASS at the PR head: a post-merge obligation (close a superseded PR, a",
    "post-deploy check) is a post-merge plan step, never a criterion.",
)

# Injected into a scanning_news brief (#3584): the shell-denied scanner
# cannot enqueue candidates itself, so it RETURNS them, and the phase evidence
# gate (``PHASE_REQUIRED_EVIDENCE["scanning_news"]``) refuses a run whose envelope
# omits ``article_suggestions``. Symmetric to ``_ANSWER_RETURN_LINES``.
_ARTICLE_SUGGESTIONS_RETURN_LINES: tuple[str, ...] = (
    "",
    "RETURN YOUR CANDIDATES AS SUGGESTIONS (this phase has no shell — do NOT try to file issues",
    "via `t3 <overlay>` or `gh`; you cannot enqueue, you HAND BACK the candidates):",
    "add an `article_suggestions` array to your final JSON result. The orchestrator queues each",
    "behind the per-article ask-gate (a PendingArticleSuggestion) for the user's approval:",
    '  "article_suggestions": [{"title": "<article title>", "url": "<article url>",',
    '                           "rationale": "<one-line why-this-matters for teatree>"}]',
    "Each item's `url` MUST be non-empty. A summary-only result with no `article_suggestions`",
    "silently drops the scan and the phase is refused, wasting the run.",
)


_REVIEWER_LIFECYCLE_SKILL = "t3:review"


def build_reviewer_dispatch_prompt(*, review_instruction: str, review_skills: list[str] | None = None) -> str:
    """Build a review sub-agent's dispatch prompt with the overlay review skills required up front.

    A review sub-agent dispatched through the Agent tool, a dynamic workflow,
    or a reviewer does not auto-load the active overlay's review
    conventions. ``build_system_context`` embeds them for the agent path,
    but an orchestrator-built dispatch prompt otherwise relies on the
    orchestrator remembering to list the skills. This shared builder prepends a
    REQUIRED "load via the Skill tool BEFORE reviewing" block — the lifecycle
    review skill plus the active overlay's review skills (deduped, order
    preserved) — so the overlay conventions reach every reviewer structurally.

    *review_skills* overrides the overlay resolution when supplied (e.g. a
    caller that already resolved the bundle); otherwise the active overlay's
    :func:`active_overlay_review_skills` are used.
    """
    from teatree.agents.skill_bundle import active_overlay_review_skills  # noqa: PLC0415 — deferred: call-time import

    resolved = review_skills if review_skills is not None else active_overlay_review_skills()
    ordered: list[str] = []
    for name in (_REVIEWER_LIFECYCLE_SKILL, *resolved):
        load_name = _explicit_load_name(name)
        if load_name not in ordered:
            ordered.append(load_name)

    lines = ["REQUIRED: Before reviewing anything, call the Skill tool for EACH of these skills:"]
    lines.extend(f"  - /{name}" for name in ordered)
    lines.extend(
        (
            "Do this FIRST — these carry the project and overlay review conventions.",
            "Reviewing without them produces false positives and misses overlay-specific rules.",
            "",
            review_instruction,
        )
    )
    return "\n".join(lines)


def _phase_fanout_directive(task: Task) -> str:
    """Render the registered fan-out directive for *task*'s ``(role, phase)``.

    Headless parity with the interactive composer
    (``loop_dispatch._task_to_dict``): both routes call the single chokepoint
    ``core.phases.resolve_fanout_directive`` so switching the harness
    between interactive and a headless runtime keeps the directive identical.
    A pair absent from the registry renders the empty string.
    """
    return resolve_fanout_directive(task.ticket.role, task.phase)


def _intake_landscape_lines(task: Task) -> tuple[str, ...]:
    """The persisted intake landscape survey block for the planner (#2541).

    The intake FSM step (``execute_provision``) baked the survey into a
    ``LandscapeArtifact``; the planner CONSUMES the latest here (as compact JSON)
    instead of re-deriving it. Empty when intake recorded none (forge outage),
    so the planner falls back to ``t3 <overlay> workspace landscape``.
    """
    return intake_survey_lines(intake_survey_json(task))


def intake_survey_lines(survey_json: str) -> tuple[str, ...]:
    if not survey_json:
        return ()
    return (
        "",
        "INTAKE LANDSCAPE SURVEY (produced by ticket-intake — CONSUME, do not re-derive):",
        "Plan AGAINST this: an open PR for the issue → finish+merge it, not fresh; a merged",
        "PR → surface for close; an in-flight worktree → build on it, never overwrite.",
        (
            "fix_first lists open defects and stuck tickets; list red or flaky gates in the touched "
            "area from live checks (or name unreadable checks). A FEATURE waits behind them (Rule 6): "
            "defer or split it, or record the owner's lift in the plan."
        ),
        survey_json,
    )


def intake_survey_json(task: Task) -> str:
    """The latest intake landscape survey as compact JSON, or ``""`` when none persisted.

    Single source of truth for the survey block string, so the byte-budget pass
    can re-derive the exact substring it needs to truncate.
    """
    from teatree.core.models.landscape_artifact import LandscapeArtifact  # noqa: PLC0415 — deferred: ORM/app-registry

    latest = LandscapeArtifact.latest_for(task.ticket)
    if latest is None:
        return ""
    return json.dumps(latest.survey, sort_keys=True)


def embedded_intake_survey_json(task: Task) -> str:
    """The survey block as it appears in *task*'s assembled context, or ``""``.

    Only :func:`_planning_phase_lines` embeds the survey, so on every other phase
    the string is not a substring of the context — offering it to the byte-budget
    pass makes it a phantom block, which can reclaim nothing. Kept here rather
    than at the budget call site so the phase that embeds the survey and the
    phase that declares it budgetable cannot drift apart.
    """
    return intake_survey_json(task) if normalize_phase(task.phase) == "planning" else ""


def _planning_phase_lines(task: Task) -> tuple[str, ...]:
    """The ``PHASE: planning`` block — intake survey (#2541), envelope directive (#3584)."""
    lines = list(_intake_landscape_lines(task))
    lines.extend(_PLAN_RETURN_LINES)
    return tuple(lines)


def _scanning_news_phase_lines() -> tuple[str, ...]:
    """The ``PHASE: scanning_news`` block — RETURN the article_suggestions envelope (#3584)."""
    return ("", "PHASE: scanning_news", *_ARTICLE_SUGGESTIONS_RETURN_LINES)


def _reviewing_phase_lines(task: Task, *, read_only_venue: bool = False) -> tuple[str, ...]:
    """The ``PHASE: reviewing`` block and its review evidence instructions."""
    plan = plan_for_task(task)
    reviewer_head_unverified = task.ticket.role == task.ticket.Role.REVIEWER and plan is None
    probe_kinds = (
        "revert-to-RED and mutation"
        if failing_run_required(gated_ticket_for_review_task(task) or task.ticket)
        else "mutation"
    )
    unverified_line = (
        "No verified live head is available for this PR. Do not review a stamped SHA or local diff; "
        "return review_venue_limit naming the live head read failure."
    )
    lines = [
        "",
        "PHASE: reviewing",
        *((unverified_line,) if reviewer_head_unverified else ()),
        (
            (
                "1. Review the DIFF UNDER REVIEW at the dispatched head; hold if it is absent or incomplete."
                if task.ticket.role == task.ticket.Role.REVIEWER
                else "1. Review the DIFF UNDER REVIEW from the mounted branch; hold if it is absent or incomplete."
            )
            if read_only_venue
            else "1. Do a thorough code review of all changes on this ticket's branch."
        ),
        ("Return required `review_context`: work item URL, documents, analysis."),
        (
            'Return `anti_vacuity`: {"ac_coverage":"<AC coverage>",'
            '"proven_tests":["<test ID observed RED before the fix>"],"no_new_tests":false}'
        ),
        (
            "`proven_tests`: IDs seen RED before the fix. Use `no_new_tests`: true and [] only if no"
            " regression test was added; other keys are dropped. Shippable maker `merge_safe` requires this;"
            " a `hold` owes none."
        ),
        ("For multi-repo tickets, review the combined diff and list all repos in `integration_review.repos`."),
        *(() if read_only_venue else _TEST_SELECTION_LINES),
        *(() if read_only_venue else (f"Run {probe_kinds} probes by pinned node id with -n0.",)),
        (
            "Use the supplied check-runs at the reviewed head for whole-tree evidence."
            if read_only_venue
            else "Read check-runs at the reviewed head for whole-tree evidence."
        ),
        *(() if read_only_venue else ("2. Run /t3:next to wrap up.",)),
        *_VERIFICATION_BRIEF_LINES,
        *(() if read_only_venue else _green_proof_binding_lines(task)),
        *_review_verdict_return_lines(assigned_reviewer_identity_for(task), read_only_venue=read_only_venue),
        *standing_hold_brief_lines(task),
        *(() if read_only_venue else rubric_brief_lines(task, plan=plan)),
        *(() if read_only_venue else declared_seams_brief_lines(task)),
        *(plan.brief_lines() if plan is not None and not read_only_venue else ()),
        *(
            ()
            if read_only_venue or reviewer_head_unverified or (plan is not None and plan.delta.case == "delta")
            else review_diff_brief_lines(task)
        ),
    ]
    return tuple(lines)


def _green_proof_binding_lines(task: Task) -> tuple[str, ...]:
    """Bind the reviewer's green-proof to the dispatched head, or ``()`` (#4720).

    ``t3 tool verify-gates`` grades whatever tree the shell is in, and a reviewer
    never leaves the main clone unless they make a worktree — so it measured
    ``origin/main`` and exited 0 while the reviewed head carried six failing
    check-runs, across eight consecutive reviews of one PR.
    """
    target = dispatch_review_target(task)
    if target is None:
        return ()
    head = target.head_sha
    if not head and target.bind_live_head:
        head = live_head_at(slug=target.slug, pr_id=target.pr_id, host_kind=target.host_kind).sha
    if not head:
        return ()
    return (
        "",
        f"GREEN-PROOF BINDING: the head under review is {head}.",
        "`t3 tool verify-gates` grades whatever tree your shell is in — from the main clone it",
        "measures the default branch and says nothing about this PR. Bind it to the head, or do",
        "not report it at all:",
        f"  t3 review checkout {task.ticket.issue_url} --sha {head} --base-dir /var/tmp",
        f"  cd <that dir> && t3 tool verify-gates --expect-sha {head}",
        "Report its exit code WITH the SHA it printed. It covers the prek hooks only — the merge",
        f"authority is the forge's check-runs at {head[:12]}, so read those before grading CI green.",
    )


def _answering_phase_lines(task: Task, *, read_only_venue: bool = False) -> tuple[str, ...]:
    """The ``PHASE: answering`` block — draft, then RETURN the answer envelope.

    The shell-denied answerer cannot post the reply itself; it hands the draft
    back and the orchestrator posts on confirmation. Surfaces the inbound thread
    context (``ticket.extra["slack_answer"]``, populated by the reactive
    slack-answer cycle) best-effort so the agent knows what ``thread_ref`` to
    fill; absent for the event-router dispatch shape, which carries the thread
    on the routed ``IncomingEvent`` the answerer skill reads.
    """
    lines = ["", "PHASE: answering", "Read the thread context and draft a concise reply in the user's voice."]
    ticket_extra = task.ticket.extra if isinstance(task.ticket.extra, dict) else {}
    slack_answer = ticket_extra.get("slack_answer")
    if isinstance(slack_answer, dict):
        thread_ts = str(slack_answer.get("slack_ts") or "")
        question = str(slack_answer.get("question") or "")
        if thread_ts:
            lines.append(f"Inbound Slack thread ts (use as `thread_ref`): {thread_ts}")
        if question:
            lines.append(f"The user's message: {question}")
    lines.extend(_ANSWER_RETURN_LINES)
    if read_only_venue:
        lines.append(
            "The inbound event is under INBOUND EVENT in the task message; you reach no thread or forge from here."
        )
    if owes_work_item(task.ticket):
        lines.extend(_WORK_ITEM_RETURN_LINES)
        if task.ticket.overlay:
            try:
                lines.extend(get_overlay_for_ticket(task.ticket).metadata.work_item_return_lines())
            except Exception:
                logger.exception("Answering work-item overlay hook failed for ticket %s", task.ticket_id)
    return tuple(lines)


def _shipping_phase_lines() -> tuple[str, ...]:
    """The ``PHASE: shipping`` auto-review-gate block."""
    reviewer_dispatch = build_reviewer_dispatch_prompt(
        review_instruction="Review the diff on this ticket's branch and report findings."
    )
    return (
        "",
        "PHASE: shipping — auto-review gate",
        "Before creating the PR, check quality gates: `t3 <overlay> pr check-gates <ticket_id>`.",
        "If `missing` includes `reviewing`:",
        "1. Spawn a reviewer sub-agent with this exact prompt, including its skill block:",
        reviewer_dispatch,
        ("2. When it completes, mark reviewing visited: `t3 <overlay> lifecycle visit-phase <ticket_id> reviewing`."),
        "3. Retry `t3 <overlay> pr create <ticket_id>`.",
        "If `missing` includes `retro`:",
        "1. Run `/t3:retro` to capture lessons from this session and commit any skill fixes.",
        "2. Mark retro as visited: `t3 <overlay> lifecycle visit-phase <ticket_id> retro`.",
        "3. Retry `t3 <overlay> pr create <ticket_id>`.",
        "Review in this session's sub-agent, not a new session.",
    )


def phase_specific_lines(
    task: Task, skills: list[str], *, stage_exclude: frozenset[str] = frozenset(), read_only_venue: bool = False
) -> tuple[str, ...]:
    """The per-phase trailing block for ``build_system_context``, or ``()``.

    Dispatches on the canonical phase token. coding/shipping carry their
    existing directives; any registered ``(role, phase)`` additionally surfaces
    its fan-out directive. One ``(role, phase)`` pair maps to one
    block — they are mutually exclusive on the canonical phase. *stage_exclude*
    keeps the phase's stage skills out of the coding force-load block (they are
    embedded in full instead).
    """
    phase = normalize_phase(task.phase)
    if phase == "coding":
        block = (
            "",
            "PHASE: coding — builder dispatch contract",
            *head_state_brief_lines(task),
            *round_brief_lines(task),
            *_coding_phase_directive(skills, stage_exclude=stage_exclude),
        )
    else:
        builder = (read_only_venue and _READ_ONLY_PHASE_BLOCK_BUILDERS.get(phase)) or _PHASE_BLOCK_BUILDERS.get(phase)
        block = builder(task) if builder is not None else ()
        if phase in _WORKER_EXECUTION_COMMANDS:
            block = (
                *block,
                "",
                f"PHASE: {phase} — execute on the current worker under its existing write approval policy.",
                _WORKER_EXECUTION_COMMANDS[phase],
            )
        if phase == "debugging":
            block = (*block, *round_brief_lines(task))
    if fanout := _phase_fanout_directive(task):
        block = (*block, fanout) if phase == "reviewing" else (*block, "", f"PHASE: {phase}", fanout)
    return (*block, *_fix_record_lines(task, phase))


#: Phases whose agent is the one FIXING the defect, so it is the actor that can state
#: the root cause. Mirrors ``transient_requeue_correction.CORRECTIVE_PHASES``.
_FIXING_PHASES = frozenset({"coding", "debugging"})

_WORKER_EXECUTION_COMMANDS = {
    "bughunt": "Inspect and reproduce candidates before reporting each finding.",
    "dogfood_smoke": "Run `t3 dogfood overlay-provision-smoke` for the configured overlay and report its result.",
    "eval_local": "Run `t3 eval run` for the configured local evaluation and report its result.",
}


def _fix_record_lines(task: Task, phase: str) -> tuple[str, ...]:
    """The FixRecord return directive for a fixing phase on a fix ticket, or the review of a fix PR, else ``()``."""
    if phase in _FIXING_PHASES and is_fix(task.ticket):
        return FIX_RECORD_RETURN_LINES if failing_run_required(task.ticket) else FIX_RECORD_NO_RED_FIELD_LINES
    if phase in ENVELOPE_VERDICT_PHASES and (gated := gated_ticket_for_review_task(task)) and is_fix(gated):
        return FIX_RECORD_REVIEW_LINES if failing_run_required(gated) else FIX_RECORD_REVIEW_NO_RED_LINES
    return ()


#: Per-phase trailing-block builders (excluding coding, which needs *skills* +
#: *stage_exclude* and stays a special case in ``phase_specific_lines``). Keyed
#: on the canonical phase token; a phase absent here carries no trailing block.
_PHASE_BLOCK_BUILDERS: dict[str, Callable[[Task], tuple[str, ...]]] = {
    "planning": _planning_phase_lines,
    "testing": _testing_phase_lines,
    "reviewing": _reviewing_phase_lines,
    "answering": _answering_phase_lines,
    "scanning_news": lambda _task: _scanning_news_phase_lines(),
    "shipping": lambda _task: _shipping_phase_lines(),
}

#: The read-only executor gets its survey in the task message, in full, so its planning block carries none.
_READ_ONLY_PHASE_BLOCK_BUILDERS: dict[str, Callable[[Task], tuple[str, ...]]] = {
    "planning": lambda _task: _PLAN_RETURN_LINES,
    "reviewing": lambda task: _reviewing_phase_lines(task, read_only_venue=True),
    "answering": lambda task: _answering_phase_lines(task, read_only_venue=True),
    "requesting_review": lambda _task: REQUESTING_REVIEW_LINES,
    "shipping": lambda _task: SHIPPING_LINES,
}
