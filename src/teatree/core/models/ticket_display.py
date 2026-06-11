"""Ticket-display rendering for the ``workspace ticket`` intake (#627).

The collapsed durable-context section and the intake summary block, split out
of ``ticket.py`` so the model file stays under the module-health LOC cap (the
ticket-display concern, anticipated by the prior in-line note).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def render_ticket_context(context: str, *, max_lines: int = 40) -> str:
    """Render ``Ticket.context`` as a collapsed intake section (#627).

    Returns a leading-newline-prefixed GitHub-style ``<details>`` block so
    the next session sees the durable knowledge without an explicit lookup,
    while the intake output stays scannable. The block is appended directly
    after the intake summary's last line, so a single ``str`` (rather than a
    line list) keeps the call site one branch-free statement. Long stores
    are truncated with a pointer to ``ticket context show``. An empty store
    renders the empty string — nothing is shown.
    """
    body = context.strip()
    if not body:
        return ""
    entries = body.splitlines()
    shown = entries[:max_lines]
    lines = ["", "", "<details>", "<summary>Ticket context (durable knowledge store)</summary>", "", *shown]
    if len(entries) > max_lines:
        hidden = len(entries) - max_lines
        lines.extend(["", f"… ({hidden} more line(s) truncated — `t3 <overlay> ticket context show`)"])
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def format_intake_summary(ticket: "Ticket", ticket_dir: str, branch: str) -> str:
    """Format the ``workspace ticket`` intake summary block (#627).

    Worktree list, ticket header, branch, and the collapsed durable-context
    section, returned as one string.
    """
    lines = [f"  {wt.repo_path}: worktree #{wt.pk}" for wt in ticket.worktrees.all()]  # ty: ignore[unresolved-attribute]
    lines.extend(
        (
            f"\nTicket #{ticket.pk} — worktrees in {ticket_dir}",
            f"  Branch: {branch}{render_ticket_context(ticket.context)}",
        )
    )
    return "\n".join(lines)
