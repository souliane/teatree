"""Ticket-display rendering for the ``workspace ticket`` intake (#627, #2892).

The collapsed durable-context / project-learnings sections and the intake
summary block, split out of ``ticket.py`` so the model file stays under the
module-health LOC cap (the ticket-display concern, anticipated by the prior
in-line note).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING

from teatree.utils.git_branch import DETACHED_HEAD

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def _render_collapsed_block(title: str, body: str, pointer_hint: str, *, max_lines: int) -> str:
    """Shared renderer behind ``render_ticket_context`` / ``render_project_learnings``.

    Returns a leading-newline-prefixed GitHub-style ``<details>`` block so
    the next session sees the durable knowledge without an explicit lookup,
    while the intake output stays scannable. Long stores are truncated with
    a pointer to *pointer_hint*. An empty *body* renders the empty string —
    nothing is shown.
    """
    stripped = body.strip()
    if not stripped:
        return ""
    entries = stripped.splitlines()
    shown = entries[:max_lines]
    lines = ["", "", "<details>", f"<summary>{title}</summary>", "", *shown]
    if len(entries) > max_lines:
        hidden = len(entries) - max_lines
        lines.extend(["", f"… ({hidden} more line(s) truncated — `{pointer_hint}`)"])
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def render_ticket_context(context: str, *, max_lines: int = 40) -> str:
    """Render ``Ticket.context`` as a collapsed intake section (#627).

    The block is appended directly after the intake summary's last line, so
    a single ``str`` (rather than a line list) keeps the call site one
    branch-free statement.
    """
    return _render_collapsed_block(
        "Ticket context (durable knowledge store)",
        context,
        "t3 <overlay> ticket context show",
        max_lines=max_lines,
    )


def render_project_learnings(content: str, *, max_lines: int = 40) -> str:
    """Render the ticket repo's ``ProjectLearning`` content as a collapsed intake section (#2892).

    The per-repo sibling of :func:`render_ticket_context`: durable knowledge
    that outlives one ticket because it is true of the *repo*, surfaced the
    same way so a fresh session sees prior project-scoped lessons without an
    explicit lookup.
    """
    return _render_collapsed_block(
        "Project learnings (durable knowledge store)",
        content,
        "t3 <overlay> learnings show <repo>",
        max_lines=max_lines,
    )


def format_intake_summary(
    ticket: "Ticket", ticket_dir: str, checked_out: Mapping[int, str], *, project_learnings: str = ""
) -> str:
    """Format the ``workspace ticket`` intake summary block (#627, #2892, #4967).

    *checked_out* maps each worktree's pk to the branch git reports for its checkout
    (``HEAD`` when detached, ``""`` when nothing readable is there), read by the
    caller so this module runs no git.
    """
    lines = [
        f"  {wt.repo_path}: worktree #{wt.pk} {_checkout_label(checked_out.get(wt.pk, ''), wt.branch)}"
        for wt in ticket.worktrees.all()
    ]
    lines.append(
        f"\nTicket #{ticket.pk} — worktrees in {ticket_dir}"
        f"{render_ticket_context(ticket.context)}{render_project_learnings(project_learnings)}"
    )
    return "\n".join(lines)


def _checkout_label(actual: str, recorded: str) -> str:
    if not actual:
        return "(no readable checkout)"
    label = "on detached HEAD" if actual == DETACHED_HEAD else f"on {actual}"
    return label if actual == recorded else f"{label} (ticket records {recorded})"
