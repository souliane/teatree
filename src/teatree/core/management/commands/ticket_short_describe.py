"""``manage.py ticket_short_describe`` — generate ``Ticket.short_description`` (#1156).

Backs the autonomous statusline-anchor description generator. Two
invocation forms: ``--ticket-id <N>`` describes one ticket (reads
``extra["issue_title"]`` and writes the generated summary back to
``ticket.short_description`` — the shape the headless
``Task(phase="short_describe")`` worker calls); ``--all-missing``
backfills every ticket with a non-blank ``extra["issue_title"]`` and a
blank ``short_description`` (useful for a one-shot CLI sweep after
rollout, before the loop has scanned each ticket).

The actual LLM call is intentionally minimal — we run ``claude -p``
with a single-shot prompt so the dependency footprint stays inside
teatree's existing agent harness. When ``claude`` is unavailable
(missing binary, sandboxed environment), the command degrades to a
truncation fallback so the field is still populated (the truncation
preserves at least the first ~40 chars of the cached title — much
better than leaving the row blank forever).
"""

import shutil
from typing import Annotated

import typer
from django.core.management.base import BaseCommand
from django_typer.management import TyperCommand, command

_PROMPT_TEMPLATE = (
    "Summarize this ticket in <=40 chars, terminal-friendly, no leading verb, no period.\n\n"
    "Ticket title: {title}\n\n"
    "Output ONLY the summary on a single line — no quotes, no prefix, no commentary."
)

_FALLBACK_LEN = 40


def _truncation_fallback(title: str) -> str:
    """Deterministic fallback when ``claude`` is unavailable.

    Returns the first ``_FALLBACK_LEN`` characters of *title* with a
    trailing Unicode ellipsis when truncated. Used when the LLM is not
    reachable so the field is at least populated.
    """
    if len(title) <= _FALLBACK_LEN:
        return title
    return title[: _FALLBACK_LEN - 1] + "…"


def _generate_short_description(title: str) -> str:
    """Generate a <=40 char description for *title* via ``claude -p``.

    Falls back to a deterministic truncation when the binary is missing
    or the subprocess fails. The fallback keeps the field non-blank so
    the scanner doesn't re-enqueue the task on the next tick.
    """
    title = title.strip()
    if not title:
        return ""
    summary = _claude_summarize(title)
    if not summary:
        return _truncation_fallback(title)
    return summary[:80]


def _claude_summarize(title: str) -> str:
    """Run ``claude -p`` and return the summary, or empty on any failure."""
    binary = shutil.which("claude")
    if binary is None:
        return ""
    from teatree.utils.run import PIPE, spawn  # noqa: PLC0415

    prompt = _PROMPT_TEMPLATE.format(title=title)
    try:
        proc = spawn([binary, "-p", prompt, "--output-format", "text"], stdout=PIPE, stderr=PIPE)
        stdout, _ = proc.communicate(timeout=30)
    except Exception:  # noqa: BLE001
        return ""
    if proc.returncode != 0:
        return ""
    output = (stdout or "").strip().splitlines()
    if not output:
        return ""
    return output[-1].strip().strip('"').strip("'")


def _describe_one(ticket_id: int, *, stdout_write) -> None:  # noqa: ANN001
    from teatree.core.models import Ticket  # noqa: PLC0415

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


def _describe_all_missing(*, stdout_write) -> None:  # noqa: ANN001
    from teatree.core.models import Ticket  # noqa: PLC0415

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


class Command(TyperCommand):
    help: str = "Generate Ticket.short_description (#1156)."

    @command(name="describe")
    def describe(
        self,
        *,
        ticket_id: Annotated[int, typer.Option("--ticket-id", help="Describe this ticket only.")] = 0,
        all_missing: Annotated[
            bool,
            typer.Option("--all-missing", help="Backfill every ticket with a tracker title and no short_description."),
        ] = False,
    ) -> None:
        """Generate AI summaries for ticket(s)."""
        if ticket_id and all_missing:
            self.stdout.write("ERROR  pass exactly one of --ticket-id or --all-missing")
            raise SystemExit(2)
        if not ticket_id and not all_missing:
            self.stdout.write("ERROR  pass exactly one of --ticket-id or --all-missing")
            raise SystemExit(2)
        if all_missing:
            _describe_all_missing(stdout_write=self.stdout.write)
        else:
            _describe_one(ticket_id, stdout_write=self.stdout.write)


__all__ = ["BaseCommand", "Command"]
