"""Build agent prompts from ticket and task context."""

from typing import cast

from teatree.agents.coding_prompt import _VERIFY_GATES_COMMAND, _coding_phase_directive, _stack_overlay_load_names
from teatree.agents.context_budget import MAX_APPEND_BYTES, enforce_budget
from teatree.agents.dispatch_preflight import declared_seams_brief_lines, head_state_brief_lines
from teatree.agents.envelope_contract import envelope_contract_lines, final_output_reminder_line
from teatree.agents.phase_blocks import intake_survey_json, phase_specific_lines
from teatree.agents.skill_injection import _explicit_load_name, _read_skill_contents, _read_skill_contents_scoped
from teatree.agents.stage_skill_prompt import stage_precedence_line, stage_skills_present
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, Ticket

# The #1135 default ``pr_review_companion``. A headless reviewer must always
# see the project review-quality bar in full, not the demoted summary.
_REVIEW_PHASE_ALWAYS_FULL = frozenset({"code-review"})

# Symmetric to the reviewer set: a headless BUILDER loses every loaded skill, so
# the enumerate-and-preserve architecture pass must embed in full, not be demoted.
_CODING_PHASE_ALWAYS_FULL = frozenset({"architecture-design"})


_MAX_PARENT_SUMMARY_LEN = 2000

# The skills pointer names the on-disk body and NOT the Skill tool: this lane is
# denied that tool, so pointing at it would name an impossible recovery.
_SURVEY_POINTER = "the intake landscape survey (re-derive with `t3 <overlay> workspace landscape`)"
_SKILLS_POINTER = "that skill's own skills/<skill>/SKILL.md — this lane has no Skill tool to load it by reference"
_PARENT_POINTER = "the parent task's recorded result"


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
            final_output_reminder_line(task.phase),
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


def build_system_context(
    task: Task, *, skills: list[str], lifecycle_skill: str = "", stage_skills: list[str] | None = None
) -> str:
    """Build the system context for headless (SDK) execution.

    When *lifecycle_skill* is provided, only the lifecycle skill and rules
    are embedded in full; companion skills get a one-line summary to save
    tokens. On the reviewing phase the active overlay's primary review skill
    and ``code-review`` are additionally embedded in full, and any remaining
    overlay review companion skills get a verbatim "load before reviewing"
    instruction, so a reviewer reviews WITH the overlay's conventions.
    *stage_skills* threads the dispatch's single overlay stage-skill resolution
    (#3206) so this builder reuses it rather than re-resolving.

    Assembled STABLE-FIRST — the fixed framing and the ~96 KB skill block lead,
    the per-task identity and prior-task result trail. Prompt caching on this lane
    is CLI-internal and exposes no ``cache_control`` surface, so prefix stability
    is the only lever teatree has over the hit rate (see
    ``_runner_options._build_options``); leading with the task identity diverges
    the cached prefix at line 2 and re-processes the whole skill block uncached on
    every dispatch. Mirrors the eval lane, which already leads with the stable
    ``SKILL_BUNDLE_FRAMING``.
    """
    lines = ["You are a TeaTree headless agent executing a task."]

    stage_present = stage_skills_present(task, skills, configured=stage_skills)
    stage_exclude = frozenset(_explicit_load_name(s) for s in stage_present)

    skill_content = ""
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

    lines.extend(
        (
            "",
            "# Context Budget",
            "- Truncate file reads to the relevant section — avoid reading entire large files.",
            "- Limit git diff output to 200 lines; use --stat for overview first.",
            "- Summarize test output instead of pasting full logs.",
            "",
            "# Authored Output — Be Concise (directive #4)",
            (
                "- Everything you write — commit/PR body, review comments, code comments, Slack —"
                " is bullets, straight to the point, no prose."
            ),
            "- Comment only the non-obvious *why*; never narrate the *what* the code already shows.",
            "- Be RIGHT but concise: trim words, never a load-bearing fact, decision, or caveat.",
            "",
            "When done, run /t3:next to wrap up (retro + the result envelope + a summary).",
            "/t3:next is a convenience, not the contract: the envelope below is required either way,",
            "so emit it yourself whenever /t3:next is unavailable or does not run.",
            *envelope_contract_lines(task.phase),
            "",
            "IMPORTANT: If you cannot proceed without human input (design decision, access, clarification),",
            "STOP immediately. Do not guess or work around it. Emit the envelope with:",
            '  {"summary": "...", "needs_user_input": true, "user_input_reason": "Why you need input"}',
            "The pipeline will automatically create an interactive session for a human to continue your work.",
        ),
    )

    lines.extend(phase_specific_lines(task, skills, stage_exclude=stage_exclude))

    lines.extend(("", f"Task ID: {task.pk}", f"Ticket: {task.ticket.ticket_number}"))

    # Context bridge: include parent task result so follow-up tasks
    # don't need full session resume to understand prior work.
    parent_summary = _parent_result_summary(task)
    if parent_summary:
        lines.extend(("", "# Prior Task Result", "", parent_summary))

    return _enforce_context_budget("\n".join(lines), task, parent_summary=parent_summary, skill_content=skill_content)


def _enforce_context_budget(text: str, task: Task, *, parent_summary: str, skill_content: str) -> str:
    """Bound the assembled append under the argv-element byte limit (E2BIG guard).

    The claude-agent-sdk passes the whole append as ONE ``--append-system-prompt``
    argv element, and Linux caps a single element at 128 KiB — an oversized
    survey/skills/parent block makes the ``claude`` spawn die with E2BIG. The
    largest uncapped blocks are truncated with a pointer marker, survey first
    (re-derivable), then skills, then the parent context last (most load-bearing
    for continuity). The survey is re-derived only on the over-budget path, so a
    normal-sized context is one build with no extra query and byte-identical
    output. The skill bundle always overruns, so it is always section-truncated —
    see :mod:`teatree.agents.context_budget`.
    """
    if len(text.encode()) <= MAX_APPEND_BYTES:
        return text
    blocks = (
        (intake_survey_json(task), _SURVEY_POINTER),
        (skill_content, _SKILLS_POINTER),
        (parent_summary, _PARENT_POINTER),
    )
    return enforce_budget(text, blocks)


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
