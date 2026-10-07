"""``t3 browser`` — drive a headless Playwright browser held open for this worktree.

``open`` launches (or reuses) the worktree's browser and loads a page, ``act`` performs one
interaction, ``inspect`` writes an accessibility snapshot, the HTML and a screenshot, and
``close`` ends the session. Every step prints what the page did: console output, page
errors, failed requests and HTTP errors.
"""

import dataclasses
import json
from collections.abc import Callable
from typing import Annotated

import typer

from teatree.browser.evidence import BrowserEvent
from teatree.browser.session import (
    NAVIGATION_TIMEOUT_S,
    BrowserError,
    BrowserSession,
    StepFailedError,
    StepReport,
    Verb,
)
from teatree.core.invocation_cwd import invocation_cwd

SNAPSHOT_PREVIEW_LINES = 200

browser_app = typer.Typer(
    name="browser",
    no_args_is_help=True,
    help="Drive a headless browser held open for this worktree (Playwright): open, act, inspect, close.",
)

JsonFlag = Annotated[bool, typer.Option("--json", help="Print the step as one JSON object.")]


@browser_app.command(name="open")
def open_page(
    url: str,
    *,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait for the page's load event.")
    ] = NAVIGATION_TIMEOUT_S,
    as_json: JsonFlag = False,
) -> None:
    """Load URL in this worktree's headless browser, launching the browser on first use."""
    _emit_step(_run(lambda session: session.open(url, timeout_s=timeout), as_json=as_json), as_json=as_json)


@browser_app.command()
def act(
    verb: Verb,
    arguments: Annotated[
        list[str] | None, typer.Argument(help="SELECTOR [VALUE|TEXT|KEY|FILE...], or EXPRESSION")
    ] = None,
    *,
    as_json: JsonFlag = False,
) -> None:
    """Perform one interaction on the open page: click, fill, type, press, upload, wait, or eval."""
    _emit_step(_run(lambda session: session.act(verb, arguments or []), as_json=as_json), as_json=as_json)


@browser_app.command()
def inspect(*, as_json: JsonFlag = False) -> None:
    """Save the page's accessibility snapshot, HTML and screenshot; print what it did since it loaded."""
    inspection = _run(lambda session: session.inspect(), as_json=as_json)
    if as_json:
        payload = {
            "url": inspection.url,
            "title": inspection.title,
            "aria_snapshot": inspection.aria_snapshot,
            "snapshot": str(inspection.snapshot_path),
            "html": str(inspection.html_path),
            "screenshot": str(inspection.screenshot_path),
            "events": [dataclasses.asdict(event) for event in inspection.events],
        }
        typer.echo(json.dumps(payload))
        return
    lines = inspection.aria_snapshot.splitlines()
    typer.echo(f"URL        {inspection.url}\nTITLE      {inspection.title}")
    typer.echo(f"SNAPSHOT   {inspection.snapshot_path}")
    typer.echo("\n".join(lines[:SNAPSHOT_PREVIEW_LINES]))
    if len(lines) > SNAPSHOT_PREVIEW_LINES:
        typer.echo(f"… {len(lines) - SNAPSHOT_PREVIEW_LINES} more line(s) in {inspection.snapshot_path}")
    typer.echo(f"HTML       {inspection.html_path}\nSCREENSHOT {inspection.screenshot_path}")
    _echo_events(inspection.events)


@browser_app.command()
def close(*, as_json: JsonFlag = False) -> None:
    """End this worktree's browser session."""
    closed = _run(lambda session: session.close(), as_json=as_json)
    if as_json:
        typer.echo(json.dumps({"closed": closed}))
        return
    typer.echo("Closed the browser session." if closed else "No open browser session.")


def _run[T](step: Callable[[BrowserSession], T], *, as_json: bool) -> T:
    try:
        return step(BrowserSession.for_directory(invocation_cwd()))
    except BrowserError as exc:
        events = exc.events if isinstance(exc, StepFailedError) else []
        if as_json:
            typer.echo(json.dumps({"error": str(exc), "events": [dataclasses.asdict(event) for event in events]}))
        else:
            _echo_events(events)
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _emit_step(report: StepReport, *, as_json: bool) -> None:
    if as_json:
        payload = dataclasses.asdict(report)
        typer.echo(json.dumps(payload, default=str))
        return
    typer.echo(f"URL        {report.url}\nTITLE      {report.title}")
    _echo_events(report.events)
    if report.result is not None:
        typer.echo(f"RESULT     {json.dumps(report.result, default=str)}")


def _echo_events(events: list[BrowserEvent]) -> None:
    for event in events:
        if event.kind == "console" or event.is_finding:
            typer.echo(event.render())
