"""Build agent prompts from ticket and task context."""

from pathlib import Path
from typing import cast

from teatree.core.models import Task, Ticket
from teatree.skill_loading import DEFAULT_SKILLS_DIR

_ALWAYS_FULL_SKILLS = frozenset({"rules"})
# The #1135 default ``pr_review_companion``. A headless reviewer must always
# see the project review-quality bar in full, not the demoted summary.
_REVIEW_PHASE_ALWAYS_FULL = frozenset({"code-review"})


def _find_skill_md(name: str, skills_dir: Path | None = None) -> Path | None:
    """Locate SKILL.md for a skill name within the skills directory."""
    sd = skills_dir if skills_dir is not None else DEFAULT_SKILLS_DIR
    candidate = sd / name / "SKILL.md"
    return candidate if candidate.is_file() else None


def _read_skill_contents(skills: list[str], *, skills_dir: Path | None = None) -> str:
    """Read and concatenate SKILL.md content for each resolved skill."""
    sd = skills_dir if skills_dir is not None else DEFAULT_SKILLS_DIR
    sections: list[str] = []
    for name in skills:
        skill_md = _find_skill_md(name, sd)
        if skill_md is not None:
            content = skill_md.read_text(encoding="utf-8")
            sections.append(f"--- SKILL: {name} ---\n{content}")
    return "\n\n".join(sections)


def _is_primary(name: str, primary_skills: set[str]) -> bool:
    """Check if a skill name (or path) matches the primary set or always-full list."""
    if name in primary_skills or name in _ALWAYS_FULL_SKILLS:
        return True
    skill_dir_name = Path(name).parent.name if "/" in name else ""
    return skill_dir_name in primary_skills or skill_dir_name in _ALWAYS_FULL_SKILLS


def _explicit_load_name(name: str) -> str:
    """Return the bare ``/skill`` reference for an explicit-load instruction."""
    return Path(name).parent.name if "/" in name else name


def _read_skill_contents_scoped(
    skills: list[str],
    *,
    primary_skills: set[str],
    explicit_load_skills: set[str] | None = None,
    skills_dir: Path | None = None,
) -> str:
    """Read skills with scoping.

    Primary skills (the lifecycle skill, ``rules``, and — on the reviewing
    phase — the overlay's primary review skills) get full content. Skills in
    *explicit_load_skills* get a verbatim "Load /<skill> via the Skill tool
    BEFORE reviewing" instruction instead of the generic, easy-to-ignore
    "available — load if needed" summary. Everything else gets the generic
    summary.
    """
    sd = skills_dir if skills_dir is not None else DEFAULT_SKILLS_DIR
    explicit = explicit_load_skills or set()
    sections: list[str] = []
    companion_names: list[str] = []
    explicit_names: list[str] = []
    for name in skills:
        if _is_primary(name, primary_skills):
            skill_md = _find_skill_md(name, sd)
            if skill_md is not None:
                content = skill_md.read_text(encoding="utf-8")
                sections.append(f"--- SKILL: {name} ---\n{content}")
        elif name in explicit or _explicit_load_name(name) in explicit:
            explicit_names.append(name)
        else:
            companion_names.append(name)
    if explicit_names:
        block = "--- REVIEW COMPANION SKILLS (REQUIRED — load before reviewing) ---\n"
        block += "\n".join(
            f"Load /{_explicit_load_name(name)} via the Skill tool BEFORE reviewing." for name in explicit_names
        )
        sections.append(block)
    if companion_names:
        summary = "--- COMPANION SKILLS (loaded but summarized to save context) ---\n"
        summary += "\n".join(f"- {name}: available — load if needed" for name in companion_names)
        sections.append(summary)
    return "\n\n".join(sections)


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


def build_task_prompt(task: Task) -> str:
    """Build a work prompt for a headless agent."""
    ticket: Ticket = task.ticket
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}

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

    # PR context
    prs = extra.get("prs", {})
    if isinstance(prs, dict) and prs:
        lines.extend(("", "Open pull requests:"))
        for pr in prs.values():
            if not isinstance(pr, dict):
                continue
            url = pr.get("url", "")
            title = pr.get("title", "")
            draft = " (draft)" if pr.get("draft") else ""
            pipeline = pr.get("pipeline_status", "")
            pipeline_info = f" — pipeline: {pipeline}" if pipeline else ""
            lines.append(f"  - {url}{draft}{pipeline_info}")
            if title:
                lines.append(f"    {title}")

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
            "5. Run tests before declaring done",
        ),
    )

    return "\n".join(lines)


def _review_phase_scoping(skills: list[str]) -> tuple[set[str], set[str]]:
    """Return ``(primary_review_skills, explicit_load_skills)`` for the reviewing phase.

    A ``claude -p`` headless reviewer does not auto-call the Skill tool, so the
    overlay's review conventions must reach it inline. The active overlay's
    review-skill set (``[pr_review_companion, *companion_skills]``) is split per
    the token budget: the PRIMARY review skill (first entry) plus ``code-review``
    embed IN FULL; any additional review companions get a verbatim
    "Load /<skill> via the Skill tool BEFORE reviewing" instruction rather than
    being demoted to the generic, ignorable "available — load if needed" summary.
    Only the review skills actually present in *skills* are scoped, so a
    companion that failed to resolve is not surfaced as required.
    """
    from teatree.agents.skill_bundle import active_overlay_review_skills  # noqa: PLC0415

    review_skills = [s for s in active_overlay_review_skills() if s in skills]
    primary: set[str] = set(_REVIEW_PHASE_ALWAYS_FULL)
    explicit: set[str] = set()
    if review_skills:
        primary.add(review_skills[0])
        explicit.update(review_skills[1:])
    explicit -= primary
    return primary, explicit


def build_system_context(task: Task, *, skills: list[str], lifecycle_skill: str = "") -> str:
    """Build the system context for headless (SDK) execution.

    When *lifecycle_skill* is provided, only the lifecycle skill and rules
    are embedded in full; companion skills get a one-line summary to save
    tokens. On the reviewing phase the active overlay's primary review skill
    and ``code-review`` are additionally embedded in full, and any remaining
    overlay review companions get a verbatim "load before reviewing"
    instruction, so a headless reviewer reviews WITH the overlay's conventions.
    """
    lines = ["You are a TeaTree headless agent executing a task."]
    lines.extend((f"Task ID: {task.pk}", f"Ticket: {task.ticket.ticket_number}"))

    # Context bridge: include parent task result so follow-up tasks
    # don't need full session resume to understand prior work.
    parent_summary = _parent_result_summary(task)
    if parent_summary:
        lines.extend(("", "# Prior Task Result", "", parent_summary))

    if skills:
        if lifecycle_skill:
            primary_skills = {lifecycle_skill}
            explicit_load_skills: set[str] | None = None
            if task.phase == "reviewing":
                review_primary, explicit_load_skills = _review_phase_scoping(skills)
                primary_skills |= review_primary
            skill_content = _read_skill_contents_scoped(
                skills,
                primary_skills=primary_skills,
                explicit_load_skills=explicit_load_skills,
            )
        else:
            skill_content = _read_skill_contents(skills)
        if skill_content:
            lines.extend(("", "# Loaded Skills", "", skill_content))

    if task.phase == "reviewing":
        lines.extend(
            (
                "",
                "PHASE: reviewing",
                "1. Do a thorough code review of all changes on this ticket's branch.",
                "2. Run /t3:next when done — it handles retro + structured result + handoff.",
            ),
        )

    if task.phase == "shipping":
        lines.extend(
            (
                "",
                "PHASE: shipping — auto-review gate",
                "Before creating the PR, check quality gates: `t3 <overlay> pr check-gates <ticket_id>`.",
                "If the result shows `reviewing` in the `missing` list:",
                "1. Spawn a sub-agent to review the diff (run the code-review skill against the branch).",
                (
                    "2. After the sub-agent completes, mark reviewing as visited:"
                    " `t3 <overlay> lifecycle visit-phase <ticket_id> reviewing`."
                ),
                "3. Retry `t3 <overlay> pr create <ticket_id>`.",
                "If the result shows `retro` in the `missing` list:",
                "1. Run `/t3:retro` to capture lessons from this session and commit any skill fixes.",
                ("2. Mark retro as visited: `t3 <overlay> lifecycle visit-phase <ticket_id> retro`."),
                "3. Retry `t3 <overlay> pr create <ticket_id>`.",
                "Do NOT create a new session for the review — use a sub-agent within this session.",
            ),
        )

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
