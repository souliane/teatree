"""``manage.py ticket_short_describe`` — generate ``Ticket.short_description`` (#1156).

Backs the autonomous statusline-anchor description generator. Two
invocation forms: ``--ticket-id <N>`` describes one ticket (reads
``extra["issue_title"]`` and writes the generated summary back to
``ticket.short_description`` — the shape the headless
``Task(phase="short_describe")`` worker calls); ``--all-missing``
backfills every ticket with a non-blank ``extra["issue_title"]`` and a
blank ``short_description`` (useful for a one-shot CLI sweep after
rollout, before the loop has scanned each ticket).

The actual LLM call is intentionally minimal — a single in-process
``claude_agent_sdk.query`` turn through the shared clean-room SDK builder
(the same :func:`teatree.eval.sdk_runner.build_sdk_options` the eval
runner and judge use), on a cheap model with no tools. No ``claude -p``
subprocess — the SDK is the one runner post the #2204 cutover. When
``claude`` is unavailable (missing binary, sandboxed environment) or the
turn fails, the command degrades to a truncation fallback so the field
is still populated (the truncation preserves at least the first ~40
chars of the cached title — much better than leaving the row blank
forever).
"""

import asyncio
import shutil
from pathlib import Path
from typing import Annotated

import typer
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
from django.core.management.base import BaseCommand
from django_typer.management import TyperCommand, command

_PROMPT_TEMPLATE = (
    "Summarize this ticket in <=40 chars, terminal-friendly, no leading verb, no period.\n\n"
    "Ticket title: {title}\n\n"
    "Output ONLY the summary on a single line — no quotes, no prefix, no commentary."
)

_SYSTEM_PROMPT = "You write terse terminal-friendly ticket summaries. Reply with the summary line only."

#: A cheap model is plenty for a ≤40-char summary — pin haiku so the
#: one-shot describe never inherits the user's expensive default.
_DESCRIBE_MODEL = "claude-haiku-4-5"

_FALLBACK_LEN = 40
_WATCHDOG_SECONDS = 30


def _truncation_fallback(title: str) -> str:
    """Deterministic fallback when the SDK is unavailable.

    Returns the first ``_FALLBACK_LEN`` characters of *title* with a
    trailing Unicode ellipsis when truncated. Used when the LLM is not
    reachable so the field is at least populated.
    """
    if len(title) <= _FALLBACK_LEN:
        return title
    return title[: _FALLBACK_LEN - 1] + "…"


def _generate_short_description(title: str) -> str:
    """Generate a <=40 char description for *title* via the Agent SDK.

    Falls back to a deterministic truncation when the binary is missing
    or the turn fails. The fallback keeps the field non-blank so the
    scanner doesn't re-enqueue the task on the next tick.
    """
    title = title.strip()
    if not title:
        return ""
    summary = _claude_summarize(title)
    if not summary:
        return _truncation_fallback(title)
    return summary[:80]


def _claude_summarize(title: str) -> str:
    """Summarize *title* via one in-process SDK turn, or empty on any failure.

    Drives :func:`claude_agent_sdk.query` once through the shared clean-room
    builder (``setting_sources=[]``, no tools, a cheap model, a 1-turn cap)
    so the developer's personal context never biases the summary. ``claude``
    absence is the same provisioning gate the SDK eval runner uses — the SDK
    spawns the CLI child, so ``shutil.which`` short-circuits when it is
    missing. Any failure (no binary, timeout, SDK error) returns ``""`` so
    the caller degrades to the truncation fallback.
    """
    if shutil.which("claude") is None:
        return ""
    prompt = _PROMPT_TEMPLATE.format(title=title)
    try:
        text = asyncio.run(_describe(prompt))
    except Exception:  # noqa: BLE001 — a summary failure must never break the backfill
        return ""
    line = text.strip().splitlines()
    if not line:
        return ""
    return line[-1].strip().strip('"').strip("'")


async def _describe(prompt: str) -> str:
    """One clean-room SDK turn for *prompt*; returns the final text, watchdog-bounded."""
    from teatree.eval.isolation import isolated_claude_env  # noqa: PLC0415
    from teatree.eval.sdk_runner import CleanRoomConfig, build_sdk_options  # noqa: PLC0415

    with isolated_claude_env() as (env, cwd):
        options = build_sdk_options(
            CleanRoomConfig(
                system_prompt=_SYSTEM_PROMPT,
                workspace=Path(cwd),
                cwd=cwd,
                env=env,
                allowed_tools=(),
                model=_DESCRIBE_MODEL,
                max_turns=1,
            )
        )
        return await asyncio.wait_for(_collect_text(prompt, options), timeout=_WATCHDOG_SECONDS)


async def _collect_text(prompt: str, options: ClaudeAgentOptions) -> str:
    """Stream the query, returning the concatenated assistant text blocks."""
    parts: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
    return "\n".join(parts)


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
