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
from collections.abc import Callable

from teatree.agents.coding_prompt import _coding_phase_directive
from teatree.agents.dispatch_preflight import (
    declared_seams_brief_lines,
    head_state_brief_lines,
    review_diff_brief_lines,
)
from teatree.agents.skill_injection import _explicit_load_name
from teatree.config.agent_spawn import resolve_agent_config
from teatree.core.modelkit.phases import normalize_phase, resolve_fanout_directive
from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE
from teatree.core.models import Task

# The anti-rubber-stamp contract for a verification brief — prove the change out
# first, then grade every quality dimension.
_VERIFICATION_BRIEF_LINES: tuple[str, ...] = (
    "",
    "VERIFICATION RIGOR (do NOT rubber-stamp):",
    "1. Read the PROOF first — reproduce the change's claimed outcome (a PoC read / a test run)",
    "   before accepting it; a summary is not evidence, and a finding you cannot reproduce is not a finding.",
    "2. Grade against the six quality dimensions and record a per-dimension verdict:",
    "   correctness | robustness (failure modes) | maintainability | coherence | reliability | proactivity.",
)

# The reviewer returns its verdict in the result envelope rather than writing the
# row itself: maker≠checker requires a different actor, so the orchestrator records
# it server-side. The phase carries the shell (``phase_tools.VERDICT_REVIEW_PHASES``).
_REVIEW_VERDICT_RETURN_LINES: tuple[str, ...] = (
    "",
    "RECORD YOUR VERDICT BY RETURNING IT (do NOT try `t3 <overlay> review record` — maker≠checker",
    "requires a different actor to write the row): add a `review_verdict` object to your final JSON",
    "result. The orchestrator records the ReviewVerdict server-side and releases the review lock:",
    '  "review_verdict": {"verdict": "merge_safe"|"hold", "reviewed_sha": "<full 40-char HEAD SHA>",',
    '                     "reviewer_identity": "<your reviewer id, NOT a maker/loop role>",',
    '                     "gh_verify_result": "green"|"pending"|"failed",',
    '                     "blast_class": "substrate"|"logic"|"docs",',
    '                     "findings": [{"severity": "...", "summary": "...", "file": "...", "line": 0}]}',
    ENVELOPE_FINDINGS_RULE,
    "`verdict` accepts ONLY merge_safe or hold — PASS/LGTM/approve are refused and record nothing.",
    "`reviewed_sha` MUST be the full 40-char SHA of the head you were DISPATCHED for (`git rev-parse HEAD`):",
    "the verdict is recorded at that head and the merge gate compares it against the forge's live head.",
    "Any other head is REFUSED and records nothing — if the head moved under you, review the dispatched",
    "one or return needs_user_input; never hand back a verdict for a tree you were not dispatched for.",
    "OMITTING `reviewed_sha` is REFUSED too: an undisclosed head is not read as agreement with the",
    "dispatched one, so a verdict that names no head records nothing at all.",
    "A result with no `review_verdict` FAILS the phase — a review that records no verdict never happened.",
)

# Injected into an answering brief: the answering phase is denied the
# shell (agents/answerer.md tools = Read/Grep/Glob only), so it CANNOT post the
# reply itself via the Replier / `t3 <overlay> notify` CLI. It RETURNS the draft
# in the result envelope instead; the orchestrator (``attempt_recorder`` →
# ``_maybe_record_answer_draft``) posts your draft directly to the owner
# in-thread — answering the owner is not a post on the owner's behalf, so it
# is never approval-gated or deferred (an on-behalf reply to a third party
# still routes through the DeferredQuestion approval path).
# Symmetric to ``_REVIEW_VERDICT_RETURN_LINES`` — without this directive the
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

# Injected into a planning brief (#3584): the phase evidence gate
# (``PHASE_REQUIRED_EVIDENCE["planning"]``) refuses a run whose result envelope
# omits ``plan_text``, so the planner MUST place the full plan under that key —
# not only as prose or a PlanArtifact. Without this reinforcing directive the
# planner produced a plan but returned a summary-only envelope, and the attempt
# was refused for "missing required evidence for phase 'planning'" then re-run.
# Symmetric to ``_REVIEW_VERDICT_RETURN_LINES`` / ``_ANSWER_RETURN_LINES``.
_PLAN_RETURN_LINES: tuple[str, ...] = (
    "",
    "RETURN YOUR PLAN IN THE ENVELOPE (the phase evidence gate refuses a run with no `plan_text`):",
    "your final JSON result MUST carry the full implementation plan under the `plan_text` key:",
    '  "plan_text": "<the complete plan — file-level changes, data model, API contracts, test',
    '                strategy, and the E2E test plan / Acceptance scenarios section when UI-visible>"',
    "A summary-only result with no `plan_text` drops the plan and the phase is refused, wasting the run.",
    "ALSO return `base_sha` and `adequacy` — under `require_plan_adequacy` PlanArtifact.record",
    "REFUSES a plan without them, and the agent lane can only supply them through this envelope:",
    '  "base_sha": "<`git rev-parse origin/<target-branch>` — the full 40-char hex HEAD you planned against>",',
    '  "adequacy": {"design": {"content": "<the approach>"},',
    '               "integration_seams": {"content": ["<registry/contract/sibling path the change touches>"]},',
    '               "edge_cases": {"content": ["<edge case>"]},',
    '               "test_strategy": {"content": "<what proves it, fail-before included>"}}',
    "Every section must be substantive OR carry an explicit reasoned negative instead of `content`",
    '(e.g. {"none_reason": "no seams: single leaf module, no registry touched"}); silence never passes.',
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
    """Render the opt-in fan-out directive for *task*'s ``(role, phase)``, or ``""``.

    Headless parity with the interactive composer
    (``loop_dispatch._task_to_dict``): both routes call the single chokepoint
    ``core.phases.resolve_fanout_directive`` so switching the harness
    between interactive and a headless runtime keeps the directive identical.
    Empty by default — ``resolve_fanout_directive`` renders nothing until the
    user opts the pair in via ``[agent.phase_fanout]`` — so a agent dispatch
    is byte-identical to today out of the box.
    """
    return resolve_fanout_directive(task.ticket.role, task.phase, resolve_agent_config())


def _intake_landscape_lines(task: Task) -> tuple[str, ...]:
    """The persisted intake landscape survey block for the planner (#2541).

    The intake FSM step (``execute_provision``) baked the survey into a
    ``LandscapeArtifact``; the planner CONSUMES the latest here (as compact JSON)
    instead of re-deriving it. Empty when intake recorded none (forge outage),
    so the planner falls back to ``t3 <overlay> workspace landscape``.
    """
    survey_json = intake_survey_json(task)
    if not survey_json:
        return ()
    return (
        "",
        "INTAKE LANDSCAPE SURVEY (produced by ticket-intake — CONSUME, do not re-derive):",
        "Plan AGAINST this: an open PR for the issue → finish+merge it, not fresh; a merged",
        "PR → surface for close; an in-flight worktree → build on it, never overwrite.",
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


def _planning_phase_lines(task: Task) -> tuple[str, ...]:
    """The ``PHASE: planning`` block — intake survey (#2541), envelope directive (#3584), opted-in fan-out."""
    lines = list(_intake_landscape_lines(task))
    lines.extend(_PLAN_RETURN_LINES)
    if fanout := _phase_fanout_directive(task):
        lines.extend(("", "PHASE: planning", fanout))
    return tuple(lines)


def _scanning_news_phase_lines() -> tuple[str, ...]:
    """The ``PHASE: scanning_news`` block — RETURN the article_suggestions envelope (#3584)."""
    return ("", "PHASE: scanning_news", *_ARTICLE_SUGGESTIONS_RETURN_LINES)


def _reviewing_phase_lines(task: Task) -> tuple[str, ...]:
    """The ``PHASE: reviewing`` block, plus an opted-in fan-out directive."""
    lines = [
        "",
        "PHASE: reviewing",
        "1. Do a thorough code review of all changes on this ticket's branch.",
        "2. Run /t3:next when done — it handles retro + structured result + handoff.",
        *_VERIFICATION_BRIEF_LINES,
        *_REVIEW_VERDICT_RETURN_LINES,
        *declared_seams_brief_lines(task),
        *review_diff_brief_lines(task),
    ]
    if fanout := _phase_fanout_directive(task):
        lines.append(fanout)
    return tuple(lines)


def _answering_phase_lines(task: Task) -> tuple[str, ...]:
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
        "If the result shows `reviewing` in the `missing` list:",
        "1. Spawn a sub-agent to review the diff. Use this exact dispatch prompt so the",
        "   reviewer loads the overlay review conventions (do NOT abbreviate the skill block):",
        reviewer_dispatch,
        (
            "2. After the sub-agent completes, mark reviewing as visited:"
            " `t3 <overlay> lifecycle visit-phase <ticket_id> reviewing`."
        ),
        "3. Retry `t3 <overlay> pr create <ticket_id>`.",
        "If the result shows `retro` in the `missing` list:",
        "1. Run `/t3:retro` to capture lessons from this session and commit any skill fixes.",
        "2. Mark retro as visited: `t3 <overlay> lifecycle visit-phase <ticket_id> retro`.",
        "3. Retry `t3 <overlay> pr create <ticket_id>`.",
        "Do NOT create a new session for the review — use a sub-agent within this session.",
    )


def phase_specific_lines(
    task: Task, skills: list[str], *, stage_exclude: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    """The per-phase trailing block for ``build_system_context``, or ``()``.

    Dispatches on the canonical phase token. coding/shipping carry their
    existing directives; planning/reviewing additionally surface an opted-in
    fan-out directive (default-OFF). One ``(role, phase)`` pair maps to one
    block — they are mutually exclusive on the canonical phase. *stage_exclude*
    keeps the phase's stage skills out of the coding force-load block (they are
    embedded in full instead).
    """
    phase = normalize_phase(task.phase)
    if phase == "coding":
        return (
            "",
            "PHASE: coding — builder dispatch contract",
            *head_state_brief_lines(task),
            *_coding_phase_directive(skills, stage_exclude=stage_exclude),
        )
    builder = _PHASE_BLOCK_BUILDERS.get(phase)
    return builder(task) if builder is not None else ()


#: Per-phase trailing-block builders (excluding coding, which needs *skills* +
#: *stage_exclude* and stays a special case in ``phase_specific_lines``). Keyed
#: on the canonical phase token; a phase absent here carries no trailing block.
_PHASE_BLOCK_BUILDERS: dict[str, Callable[[Task], tuple[str, ...]]] = {
    "planning": _planning_phase_lines,
    "reviewing": _reviewing_phase_lines,
    "answering": _answering_phase_lines,
    "scanning_news": lambda _task: _scanning_news_phase_lines(),
    "shipping": lambda _task: _shipping_phase_lines(),
}
