"""Generate ``Ticket.short_description`` — the statusline-anchor summariser (#1156).

A <=40 char, terminal-friendly summary for a ticket, produced by one clean-room,
cheap-tier turn through the shared one-shot seam
(:func:`teatree.agents.one_shot.run_one_shot`): the ``cheap`` tier is resolved to a
concrete model id and routed through the active harness (``claude_sdk`` or
``pydantic_ai``/OrcaRouter), so the summary follows a swapped tier-model DB row and
works off-Claude — never a hardcoded model id, and no ``teatree.eval`` import on the
production path. When the model is unavailable (missing binary, sandboxed environment)
or the turn fails, the seam returns ``None`` and we degrade to a truncation fallback so
the field is still populated (much better than leaving the row blank forever). A refused
ambient environment raises instead of degrading, so a misrouted base URL surfaces rather
than silently truncating every row.

``run_short_describe`` is the deterministic-phase runner registered for the
``short_describe`` ``Task`` phase (:mod:`teatree.core.deterministic_dispatch`): the
headless worker executes it directly instead of handing the phase a generic ticket-work
brief its empty toolset cannot satisfy. It lives here — the agents layer — because the
summariser calls the one-shot LLM seam, which the ``teatree.core`` domain layer may not
import; ``teatree.agents.apps`` registers it into the domain dispatch seam at app-ready,
mirroring the headless-runner inversion.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.agents.one_shot import OneShotSpec, run_one_shot

if TYPE_CHECKING:
    from teatree.core.models import Task

_PROMPT_TEMPLATE = (
    "Summarize this ticket in <=40 chars, terminal-friendly, no leading verb, no period.\n\n"
    "Ticket title: {title}\n\n"
    "Output ONLY the summary on a single line — no quotes, no prefix, no commentary."
)

_SYSTEM_PROMPT = "You write terse terminal-friendly ticket summaries. Reply with the summary line only."

_FALLBACK_LEN = 40
_WATCHDOG_SECONDS = 30


def _truncation_fallback(title: str) -> str:
    """Deterministic fallback when the SDK is unavailable.

    Returns the first ``_FALLBACK_LEN`` characters of *title* with a trailing Unicode
    ellipsis when truncated. Used when the LLM is not reachable so the field is at least
    populated.
    """
    if len(title) <= _FALLBACK_LEN:
        return title
    return title[: _FALLBACK_LEN - 1] + "…"


def _generate_short_description(title: str) -> str:
    """Generate a <=40 char description for *title* via the Agent SDK.

    Falls back to a deterministic truncation when the binary is missing or the turn
    fails. The fallback keeps the field non-blank so the scanner doesn't re-enqueue the
    task on the next tick.
    """
    title = title.strip()
    if not title:
        return ""
    summary = _summarize(title)
    if not summary:
        return _truncation_fallback(title)
    return summary[:80]


def _summarize(title: str) -> str:
    """Summarize *title* via one clean-room, cheap-tier turn, or empty on any failure.

    Routes a single clean-room turn through the shared one-shot seam
    (:func:`teatree.agents.one_shot.run_one_shot`): the ``cheap`` tier resolved to a
    concrete model id and driven through the active harness, so the summary follows a
    swapped tier-model DB row and works off-Claude. The seam returns ``None`` on a failed
    turn (no binary, timeout, backend error), which maps to ``""`` here so the caller
    degrades to the truncation fallback; a refused ambient environment raises
    :class:`~teatree.llm.credentials.CredentialError` through instead. The model's reply
    is one line; take the LAST non-blank line and strip surrounding quotes.
    """
    prompt = _PROMPT_TEMPLATE.format(title=title)
    answer = run_one_shot(
        prompt,
        OneShotSpec(system_prompt=_SYSTEM_PROMPT, tier="cheap", max_turns=1, timeout_seconds=_WATCHDOG_SECONDS),
    )
    if not answer:
        return ""
    lines = answer.splitlines()
    if not lines:
        return ""
    return lines[-1].strip().strip('"').strip("'")


def describe_ticket(ticket_id: int, *, stdout_write: Callable[[str], object]) -> None:
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    ticket = Ticket.objects.filter(pk=ticket_id).first()
    if ticket is None:
        stdout_write(f"NOOP  no ticket with id={ticket_id}")
        raise SystemExit(1)
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    title = extra.get("issue_title", "") if isinstance(extra, dict) else ""
    title = title if isinstance(title, str) else ""
    if not title:
        stdout_write(f"NOOP  ticket {ticket_id} has no extra['issue_title'] — skipped")
        return
    summary = _generate_short_description(title)
    Ticket.objects.filter(pk=ticket.pk).update(short_description=summary)
    stdout_write(f"OK    ticket {ticket_id}: short_description={summary!r}")


def describe_all_missing(*, stdout_write: Callable[[str], object]) -> None:
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    qs = Ticket.objects.filter(short_description="").exclude(extra__issue_title="")
    count = 0
    for ticket in qs:
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        title = extra.get("issue_title", "") if isinstance(extra, dict) else ""
        title = title if isinstance(title, str) else ""
        if not title:
            continue
        summary = _generate_short_description(title)
        Ticket.objects.filter(pk=ticket.pk).update(short_description=summary)
        stdout_write(f"OK    ticket {ticket.pk}: short_description={summary!r}")
        count += 1
    stdout_write(f"DONE  described {count} ticket(s)")


def run_short_describe(task: "Task") -> str:
    """Deterministic ``short_describe`` runner: describe the task's ticket, return the log line(s)."""
    lines: list[str] = []
    describe_ticket(int(task.ticket_id), stdout_write=lines.append)  # ty: ignore[unresolved-attribute]
    return "\n".join(lines)


__all__ = ["describe_all_missing", "describe_ticket", "run_short_describe"]
