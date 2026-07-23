"""Generate ``Ticket.short_description`` — the single writer of the field (#1156).

The domain half of the short-describe phase: one cheap-tier clean-room turn
through the shared one-shot seam (:func:`teatree.agents.one_shot.run_one_shot`),
which resolves the tier to a concrete model id and routes it through the active
harness — so the summary follows a swapped tier-model DB row and works off-Claude,
never a hardcoded model id. When the model is unavailable (missing binary,
sandboxed environment) or the turn fails, the seam returns ``None`` and this
degrades to a truncation fallback so the field is still populated (much better
than leaving the row blank forever). A refused ambient environment raises instead
of degrading, so a misrouted base URL surfaces rather than silently truncating
every row.

It lives in the agents layer because it drives a model turn, and it reaches the
headless dispatch the same way the headless runner does: registered into
:mod:`teatree.core.deterministic_phases` at app-ready, so ``core`` never imports
``agents``. Both consumers share it — the ``manage.py ticket_short_describe`` CLI
and the ``short_describe`` phase whose wiring #3570 was missing.
"""

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


class TicketNotFoundError(LookupError):
    """No ticket with the requested id exists."""


def _truncation_fallback(title: str) -> str:
    """Deterministic fallback when the SDK is unavailable.

    Returns the first ``_FALLBACK_LEN`` characters of *title* with a trailing
    Unicode ellipsis when truncated, so the field is at least populated.
    """
    if len(title) <= _FALLBACK_LEN:
        return title
    return title[: _FALLBACK_LEN - 1] + "…"


def _summarize(title: str) -> str:
    """Summarize *title* via one clean-room, cheap-tier turn, or empty on any failure.

    The seam returns ``None`` on a failed turn (no binary, timeout, backend error),
    which maps to ``""`` here so the caller degrades to the truncation fallback; a
    refused ambient environment raises
    :class:`~teatree.llm.credentials.CredentialError` through instead. The model's
    reply is one line; take the LAST non-blank line and strip surrounding quotes.
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


def generate_short_description(title: str) -> str:
    """A <=40 char description for *title*, or ``""`` for a blank title."""
    title = title.strip()
    if not title:
        return ""
    summary = _summarize(title)
    if not summary:
        return _truncation_fallback(title)
    return summary[:80]


def describe_ticket_short_description(ticket_id: int) -> str:
    """Generate, persist and return ``Ticket.short_description`` for *ticket_id*.

    Returns ``""`` for a ticket with no cached ``extra["issue_title"]`` (nothing
    to summarize, field left untouched); raises :class:`TicketNotFoundError` when
    the ticket is gone.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    ticket = Ticket.objects.filter(pk=ticket_id).first()
    if ticket is None:
        msg = f"no ticket with id={ticket_id}"
        raise TicketNotFoundError(msg)
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    title = extra.get("issue_title", "")
    title = title if isinstance(title, str) else ""
    if not title:
        return ""
    summary = generate_short_description(title)
    Ticket.objects.filter(pk=ticket.pk).update(short_description=summary)
    return summary


def run_short_describe(task: "Task") -> str:
    """The ``short_describe`` deterministic-phase runner: describe the task's ticket."""
    ticket_id = task.ticket_id  # ty: ignore[unresolved-attribute]
    summary = describe_ticket_short_description(int(ticket_id))
    if not summary:
        return f"NOOP  ticket {ticket_id} has no extra['issue_title'] — skipped"
    return f"OK    ticket {ticket_id}: short_description={summary!r}"


__all__ = [
    "TicketNotFoundError",
    "describe_ticket_short_description",
    "generate_short_description",
    "run_short_describe",
]
