"""Management-command runners for the teatree-own MCP write tools.

Carved out of ``write_tools.py`` to hold that handler module under the 500-LOC
module-health cap. Each runner invokes ``django.core.management.call_command``
— the literal CLI code path every write handler routes through — and converts
the CLI's ``SystemExit`` / ``typer.Exit`` primitive into a structured
``RuntimeError`` so a FastMCP tool call is never crashed by it.
"""

import contextlib
import io
import json
from typing import Any, cast

import typer
from django.core.management import call_command


def run_command(command: str, *args: object, **kwargs: object) -> object:
    """Run a management command as the CLI does, but surface its error primitive.

    The wrapped commands signal input errors with ``SystemExit`` / ``typer.Exit``
    — a ``BaseException``. FastMCP only converts ``Exception`` to a structured
    ``ToolError``, so an unguarded exit would crash the whole tool call instead of
    returning the documented refusal. Capture the command's stderr and re-raise as
    a plain ``RuntimeError`` so the caller gets the message, not a dead session.
    """
    err = io.StringIO()
    try:
        return call_command(command, *args, stderr=err, **kwargs)
    except (SystemExit, typer.Exit) as exc:
        code = getattr(exc, "code", None)
        if code is None:
            code = getattr(exc, "exit_code", 1)
        message = err.getvalue().strip() or f"command failed (exit {code})"
        raise RuntimeError(message) from exc


def _last_json_object(text: str) -> dict[str, Any] | None:
    """The last stdout line that parses as a JSON object, or ``None``."""
    for raw in reversed(text.strip().splitlines()):
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            return cast("dict[str, Any]", json.loads(line))
    return None


def run_emitting_command(command: str, *args: object, **kwargs: object) -> dict[str, Any]:
    """Run a command that reports its verdict via one JSON line + ``SystemExit``.

    ``review_request_post`` prints a single machine-legible JSON dict
    (``action`` ∈ post/draft/suppress/refused) to stdout and terminates via
    ``SystemExit`` (0 for post/draft/suppress, 2 for refused). Capture stdout and
    return the parsed verdict — the ``action`` field carries the outcome, so the
    exit code is not needed. Surface stderr as a structured ``RuntimeError`` when
    the command emitted no JSON (so a FastMCP tool call is never crashed by the
    ``SystemExit`` primitive the CLI uses).
    """
    out = io.StringIO()
    err = io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        contextlib.suppress(SystemExit, typer.Exit),
    ):
        call_command(command, *args, **kwargs)
    payload = _last_json_object(out.getvalue())
    if payload is not None:
        return payload
    message = err.getvalue().strip() or out.getvalue().strip() or f"{command} produced no machine-readable output"
    raise RuntimeError(message)
