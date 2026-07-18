"""Build agent prompts from ticket and task context."""

import json
from typing import cast

from teatree.agents.coding_prompt import _VERIFY_GATES_COMMAND, _coding_phase_directive, _stack_overlay_load_names
from teatree.agents.dispatch_preflight import (
    declared_seams_brief_lines,
    head_state_brief_lines,
    review_diff_brief_lines,
)
from teatree.agents.skill_injection import _explicit_load_name, _read_skill_contents, _read_skill_contents_scoped
from teatree.agents.stage_skill_prompt import stage_precedence_line, stage_skills_present
from teatree.config.agent_spawn import resolve_agent_config
from teatree.core.modelkit.phases import normalize_phase, resolve_fanout_directive
from teatree.core.models import Task, Ticket

# The #1135 default ``pr_review_companion``. A headless reviewer must always
# see the project review-quality bar in full, not the demoted summary.
_REVIEW_PHASE_ALWAYS_FULL = frozenset({"code-review"})

# Symmetric to the reviewer set: a headless BUILDER loses every loaded skill, so
# the enumerate-and-preserve architecture pass must embed in full, not be demoted.
_CODING_PHASE_ALWAYS_FULL = frozenset({"architecture-design"})


_MAX_PARENT_SUMMARY_LEN = 2000


def _parent_result_summary(task: Task) -> str:
    """Return a compact summary from the parent task's last attempt result."""
    parent = task.parent_task
    if parent is None:
        return ""
    last_attempt = parent.attempts.order_by("-pk").first()
    if last_attempt is None:
        return ""
    result = last_attempt.result if isinstance(last_attempt.result, dict) else {}
    parts: list[str] = []
    if summary := str(result.get("summary", "")):
        parts.append(f"Summary: {summary[:_MAX_PARENT_SUMMARY_LEN]}")
    if files := result.get("files_modified"):
        parts.append(f"Files modified: {', '.join(str(f) for f in files[:20])}")
    if steps := result.get("next_steps"):
        parts.append(f"Next steps: {', '.join(str(s) for s in steps[:10])}")
    return "\n".join(parts)


# Injected into a verification (review) brief (PR-12): the anti-rubber-stamp
# contract — prove the change out first, then grade every quality dimension.
_VERIFICATION_BRIEF_LINES: tuple[str, ...] = (
    "",
    "VERIFICATION RIGOR (do NOT rubber-stamp):",
    "1. Read the PROOF first — reproduce the change's claimed outcome (a PoC read / a test run)",
    "   before accepting it; a summary is not evidence, and a finding you cannot reproduce is not a finding.",
    "2. Grade against the six quality dimensions and record a per-dimension verdict:",
    "   correctness | robustness (failure modes) | maintainability | coherence | reliability | proactivity.",
)

# Injected into a headless reviewing brief (corr-11): the reviewing phase is
# denied the shell (PR-11), so it CANNOT run `t3 <overlay> review record`. It returns the
# verdict in the result envelope instead; the orchestrator records it server-
# side (maker≠checker: a different actor writes the row).
_REVIEW_VERDICT_RETURN_LINES: tuple[str, ...] = (
    "",
    "RECORD YOUR VERDICT BY RETURNING IT (this phase has no shell — do NOT try `t3 <overlay> review record`):",
    "add a `review_verdict` object to your final JSON result. The orchestrator records the",
    "ReviewVerdict server-side and releases the review lock:",
    '  "review_verdict": {"verdict": "merge_safe"|"hold", "reviewed_sha": "<full 40-char HEAD SHA>",',
    '                     "reviewer_identity": "<your reviewer id, NOT a maker/loop role>",',
    '                     "gh_verify_result": "green"|"pending"|"failed",',
    '                     "findings": [{"severity": "...", "summary": "...", "file": "...", "line": 0}]}',
    "Use verdict=hold with the blocking findings when the change must not merge yet.",
)

# Injected into a headless answering brief: the answering phase is denied the
# shell (agents/answerer.md tools = Read/Grep/Glob only), so it CANNOT post the
# reply itself via the Replier / `t3 <overlay> notify` CLI. It RETURNS the draft
# in the result envelope instead; the orchestrator (``attempt_recorder`` →
# ``_maybe_record_answer_draft``) routes it through the DeferredQuestion approval
# path and posts on the user's behalf (maker≠checker: a different actor posts).
# Symmetric to ``_REVIEW_VERDICT_RETURN_LINES`` — without this directive the
# shell-denied answerer returns a prose summary with no ``answer`` field and the
# phase evidence gate refuses ("missing required evidence for phase 'answering'").
_ANSWER_RETURN_LINES: tuple[str, ...] = (
    "",
    "RETURN YOUR REPLY AS A DRAFT (this phase has no shell — do NOT try to post via",
    "`t3 <overlay> notify`, the Replier, or any CLI; you cannot post, you HAND BACK the draft):",
    "add an `answer` object to your final JSON result. The orchestrator routes it through the",
    "approval path and posts on the user's behalf:",
    '  "answer": {"text": "<the drafted reply, in the user\'s voice — no AI signature>",',
    '             "thread_ref": "<the inbound thread ts/ref this reply targets, or \'\' if none>"}',
    "The `text` MUST be non-empty — a summary-only result with no `answer` drops the reply and",
    "the phase is refused. If you cannot answer (missing context, a decision only the user can",
    "make), draft a clarifying-question reply as the `answer` text rather than returning nothing.",
)


def _task_header_lines(task: Task, extra: dict) -> list[str]:
    """Return the ticket/issue/title/labels/phase/reason header lines."""
    ticket: Ticket = task.ticket
    lines = [f"Work on ticket {ticket.ticket_number}."]
    if ticket.issue_url:
        lines.append(f"Issue: {ticket.issue_url}")
    if title := extra.get("issue_title"):
        lines.append(f"Title: {title}")
    if labels := extra.get("labels"):
        lines.append(f"Labels: {', '.join(labels)}")
    if task.phase:
        lines.append(f"Current phase: {task.phase}")
    if task.execution_reason:
        lines.append(f"Reason: {task.execution_reason}")
    return lines


def build_task_prompt(task: Task, *, skills: list[str] | None = None, stage_skills: list[str] | None = None) -> str:
    """Build a work prompt for a headless agent.

    *skills* is the resolved skill bundle for the dispatch; on the coding phase
    its framework + overlay entries are injected as an explicit skill-load
    block so a code-touching dispatch never relies on auto-detect (#1368).
    *stage_skills* threads the dispatch's single overlay stage-skill resolution
    (#3206) so this builder reuses it rather than re-resolving.
    """
    ticket: Ticket = task.ticket
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}

    lines = _task_header_lines(task, extra)
    lines.extend(_format_pr_context(extra))

    lines.extend(
        (
            "",
            "Instructions:",
            "1. Check what has been done so far (git log, existing code, PR status)",
            "2. Identify what remains to be done",
            "3. If you can proceed (code, test, fix) — do it",
            "4. If you need human input (design decision, access, clarification) — STOP immediately.",
            '   Do NOT attempt to guess or work around it. Set "needs_user_input": true and "user_input_reason": "..."',
            "   in your JSON result. The pipeline will create an interactive session for a human to continue.",
            f"5. Before declaring done, run the FULL CI-equivalent local gate set: `{_VERIFY_GATES_COMMAND}`.",
            "   It runs BOTH commit-stage and push-stage hooks; a bare `prek run --all-files` SKIPS the",
            "   push-stage gates CI re-runs. Report its exit code as the green-proof.",
        ),
    )

    if normalize_phase(task.phase) == "coding":
        present = stage_skills_present(task, skills or [], configured=stage_skills)
        stage_exclude = frozenset(_explicit_load_name(s) for s in present)
        lines.extend(
            (
                "",
                "PHASE: coding",
                *head_state_brief_lines(task),
                *declared_seams_brief_lines(task),
                *_coding_phase_directive(skills, stage_exclude=stage_exclude),
            )
        )

    return "\n".join(lines)


def _review_phase_scoping(skills: list[str]) -> tuple[set[str], set[str]]:
    """Return ``(primary_review_skills, explicit_load_skills)`` for the reviewing phase.

    A ``claude -p`` headless reviewer does not auto-call the Skill tool, so the
    overlay's review conventions must reach it inline. The active overlay's
    review-skill set (``[pr_review_companion, *companion_skills]``) is split per
    the token budget: the PRIMARY review skill (first entry) plus ``code-review``
    embed IN FULL; any additional review companion skills get a verbatim
    "Load /<skill> via the Skill tool BEFORE reviewing" instruction rather than
    being demoted to the generic, ignorable "available — load if needed" summary.
    Only the review skills actually present in *skills* are scoped, so a
    companion that failed to resolve is not surfaced as required.
    """
    from teatree.agents.skill_bundle import active_overlay_review_skills  # noqa: PLC0415 — deferred: call-time import

    review_skills = [s for s in active_overlay_review_skills() if s in skills]
    primary: set[str] = set(_REVIEW_PHASE_ALWAYS_FULL)
    explicit: set[str] = set()
    if review_skills:
        primary.add(review_skills[0])
        explicit.update(review_skills[1:])
    explicit -= primary
    return primary, explicit


_REVIEWER_LIFECYCLE_SKILL = "t3:review"


def build_reviewer_dispatch_prompt(*, review_instruction: str, review_skills: list[str] | None = None) -> str:
    """Build a review sub-agent's dispatch prompt with the overlay review skills required up front.

    A review sub-agent dispatched through the Agent tool, a dynamic workflow,
    or a headless reviewer does not auto-load the active overlay's review
    conventions. ``build_system_context`` embeds them for the headless path,
    but an orchestrator-built dispatch prompt previously relied on the
    orchestrator remembering to list the skills. This shared builder prepends a
    REQUIRED "load via the Skill tool BEFORE reviewing" block — the lifecycle
    review skill plus the active overlay's review skills (deduped, order
    preserved) — so the overlay conventions reach every reviewer structurally,
    which the ``subagent_skill_gate`` TaskCreated gate enforces on a fan-out.

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
    ``core.phases.resolve_fanout_directive`` so switching ``agent_runtime``
    between interactive and a headless runtime keeps the directive identical.
    Empty by default — ``resolve_fanout_directive`` renders nothing until the
    user opts the pair in via ``[agent.phase_fanout]`` — so a headless dispatch
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
    from teatree.core.models.landscape_artifact import LandscapeArtifact  # noqa: PLC0415 — deferred: ORM/app-registry

    latest = LandscapeArtifact.latest_for(task.ticket)
    if latest is None:
        return ()
    return (
        "",
        "INTAKE LANDSCAPE SURVEY (produced by ticket-intake — CONSUME, do not re-derive):",
        "Plan AGAINST this: an open PR for the issue → finish+merge it, not fresh; a merged",
        "PR → surface for close; an in-flight worktree → build on it, never overwrite.",
        json.dumps(latest.survey, sort_keys=True),
    )


def _planning_phase_lines(task: Task) -> tuple[str, ...]:
    """The headless ``PHASE: planning`` block — intake survey (#2541) + opted-in fan-out."""
    lines = list(_intake_landscape_lines(task))
    if fanout := _phase_fanout_directive(task):
        lines.extend(("", "PHASE: planning", fanout))
    return tuple(lines)


def _reviewing_phase_lines(task: Task) -> tuple[str, ...]:
    """The headless ``PHASE: reviewing`` block, plus an opted-in fan-out directive."""
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
    """The headless ``PHASE: answering`` block — draft, then RETURN the answer envelope.

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
    """The headless ``PHASE: shipping`` auto-review-gate block."""
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


def _phase_specific_lines(
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
    if phase == "planning":
        return _planning_phase_lines(task)
    if phase == "reviewing":
        return _reviewing_phase_lines(task)
    if phase == "answering":
        return _answering_phase_lines(task)
    if phase == "shipping":
        return _shipping_phase_lines()
    return ()


def build_system_context(
    task: Task, *, skills: list[str], lifecycle_skill: str = "", stage_skills: list[str] | None = None
) -> str:
    """Build the system context for headless (SDK) execution.

    When *lifecycle_skill* is provided, only the lifecycle skill and rules
    are embedded in full; companion skills get a one-line summary to save
    tokens. On the reviewing phase the active overlay's primary review skill
    and ``code-review`` are additionally embedded in full, and any remaining
    overlay review companion skills get a verbatim "load before reviewing"
    instruction, so a headless reviewer reviews WITH the overlay's conventions.
    *stage_skills* threads the dispatch's single overlay stage-skill resolution
    (#3206) so this builder reuses it rather than re-resolving.
    """
    lines = ["You are a TeaTree headless agent executing a task."]
    lines.extend((f"Task ID: {task.pk}", f"Ticket: {task.ticket.ticket_number}"))

    # Context bridge: include parent task result so follow-up tasks
    # don't need full session resume to understand prior work.
    parent_summary = _parent_result_summary(task)
    if parent_summary:
        lines.extend(("", "# Prior Task Result", "", parent_summary))

    stage_present = stage_skills_present(task, skills, configured=stage_skills)
    stage_exclude = frozenset(_explicit_load_name(s) for s in stage_present)

    if skills:
        if lifecycle_skill:
            # Stage skills embed IN FULL — a no-Skill-tool maker cannot load them
            # by reference, so they are primary alongside the lifecycle skill.
            primary_skills = {lifecycle_skill, *stage_present}
            explicit_load_skills: set[str] | None = None
            suppress_names: set[str] | None = None
            phase = normalize_phase(task.phase)
            if phase == "reviewing":
                review_primary, explicit_load_skills = _review_phase_scoping(skills)
                primary_skills |= review_primary
            elif phase == "coding":
                # Embed the architecture pass in full (see _CODING_PHASE_ALWAYS_FULL),
                # not the ignorable "load if needed" summary the builder would skip.
                primary_skills |= _CODING_PHASE_ALWAYS_FULL
                # The directive force-loads the stack + overlay skills (#1368);
                # drop them from the ignorable summary so it cannot contradict it.
                # Stage skills are primary/full-embed, so excluded from the block.
                suppress_names = set(_stack_overlay_load_names(skills, exclude=stage_exclude))
            skill_content = _read_skill_contents_scoped(
                skills,
                primary_skills=primary_skills,
                explicit_load_skills=explicit_load_skills,
                suppress_names=suppress_names,
            )
        else:
            skill_content = _read_skill_contents(skills)
        if skill_content:
            lines.extend(("", "# Loaded Skills", "", skill_content))
            if stage_present:
                lines.extend(("", stage_precedence_line(stage_present)))

    lines.extend(_phase_specific_lines(task, skills, stage_exclude=stage_exclude))

    lines.extend(
        (
            "",
            "# Context Budget",
            "- Truncate file reads to the relevant section — avoid reading entire large files.",
            "- Limit git diff output to 200 lines; use --stat for overview first.",
            "- Summarize test output instead of pasting full logs.",
            "",
            "When done, run /t3:next to wrap up. It will:",
            "- Run /t3:retro (captures lessons while context is fresh)",
            "- Emit the structured JSON result the pipeline needs",
            "- Display a summary of what happened",
            "",
            "If /t3:next is not available, output a JSON object on the last line:",
            '  {"summary": "...", "needs_user_input": false, "files_modified": [...], "next_steps": [...]}',
            "",
            "IMPORTANT: If you cannot proceed without human input (design decision, access, clarification),",
            "STOP immediately. Do not guess or work around it. Output:",
            '  {"summary": "...", "needs_user_input": true, "user_input_reason": "Why you need input"}',
            "The pipeline will automatically create an interactive session for a human to continue your work.",
        ),
    )

    return "\n".join(lines)


type _TicketExtra = dict[str, object]
type _PrDict = dict[str, object]


def _format_pr_context(extra: _TicketExtra) -> list[str]:
    prs = extra.get("prs", {})
    if not isinstance(prs, dict) or not prs:
        return []
    lines = ["", "Open pull requests:"]
    for raw_pr in prs.values():
        if not isinstance(raw_pr, dict):
            continue
        pr = cast("_PrDict", raw_pr)
        url = pr.get("url", "")
        pr_title = pr.get("title", "")
        draft = " (draft)" if pr.get("draft") else ""
        pipeline = pr.get("pipeline_status", "")
        pipeline_info = f" — pipeline: {pipeline}" if pipeline else ""
        lines.append(f"  - {url}{draft}{pipeline_info}")
        if pr_title:
            lines.append(f"    {pr_title}")
    return lines


def build_interactive_context(task: Task, *, skills: list[str]) -> str:
    """Build the system context for interactive Claude Code sessions."""
    ticket: Ticket = task.ticket
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}

    lines = ["You are working in an interactive TeaTree session."]
    lines.extend((f"Task ID: {task.pk}", f"Ticket: {ticket.ticket_number}"))

    if ticket.issue_url:
        lines.append(f"Issue: {ticket.issue_url}")

    if title := extra.get("issue_title"):
        lines.append(f"Title: {title}")

    if task.phase:
        lines.append(f"Phase: {task.phase}")

    if task.execution_reason:
        lines.extend(("", f"What to do: {task.execution_reason}"))

    if skills:
        lines.extend(
            (
                "",
                "REQUIRED: Before starting any work, call the Skill tool for EACH of these skills:",
                *(f"  - /{skill}" for skill in skills),
                "Do this FIRST, before reading files, running commands, or responding to the user.",
            ),
        )

    lines.extend(_format_pr_context(extra))

    lines.extend(("", "This is an interactive session — the user is present."))

    if task.execution_reason:
        lines.extend(
            (
                "Your FIRST message must present your diagnosis of the problem described above",
                "and your proposed fix. Do NOT ask the user what happened — you already have",
                "the error context. Lead with the analysis, then act.",
                "Before ending, run /t3:next — it handles retro, result reporting, and pipeline handoff.",
            ),
        )
    else:
        lines.extend(
            (
                "Your FIRST message must acknowledge the project and ticket you are working on.",
                "Summarize: ticket number, current state, what was done so far, and what you plan to do next.",
                "Then either begin working or ask the user for guidance.",
                "Before ending, run /t3:next — it handles retro, result reporting, and pipeline handoff.",
            ),
        )

    return "\n".join(lines)
