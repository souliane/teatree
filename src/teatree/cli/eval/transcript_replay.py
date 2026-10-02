"""``t3 eval transcript-replay`` — replay a real session against invariants.

Holds the replay command plus the file-resolution concern (``--file`` /
``--session`` / ``--latest`` scoped to the cwd's project), so the command body
stays a thin coordinator next to the resolver it drives.
"""

import sys
from pathlib import Path

import typer

from teatree.eval.session_transcript import parse_session_jsonl
from teatree.eval.transcript_conformance import InvariantResult, render_report, render_report_json, replay
from teatree.eval.transcript_resolver import resolve_transcript

__all__ = [
    "replay_transcript_for_all",
    "resolve_transcript",
    "transcript_replay",
]


def replay_transcript_for_all() -> list[InvariantResult] | None:
    """Replay the latest in-scope session transcript for ``t3 eval``.

    Returns ``None`` when no transcript is in scope so the all-lanes orchestrator
    renders a SKIP rather than a FAIL — a missing real run is not a violation.
    """
    transcript = resolve_transcript(latest=True, session=None, file=None)
    if transcript is None:
        return None
    events = parse_session_jsonl(transcript.read_text(encoding="utf-8", errors="replace"))
    return replay(events)


def transcript_replay(
    latest: bool = typer.Option(True, "--latest/--no-latest", help="Replay the newest session for the cwd's project."),  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
    session: str | None = typer.Option(None, "--session", help="Replay a specific session id (in the cwd's project)."),
    file: Path | None = typer.Option(None, "--file", help="Replay a specific session JSONL file path."),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Replay a real session transcript against teatree behavioural invariants.

    The #169 complement to the #168 gate-liveness corpus: #168 proves the gates
    CAN fire on synthetic payloads; this proves they DID (or weren't needed) in
    a REAL run. Django-free, stdout-only, no transport: privacy by construction.
    Exits non-zero on any invariant violation; skips and exits 0 when no
    transcript is found. The report names only invariant ids and event indexes —
    never a tool input, prompt, hook output, or quote.
    """
    transcript = resolve_transcript(latest=latest, session=session, file=file)
    if transcript is None:
        typer.echo("SKIP transcript-replay: no session transcript found in scope", err=True)
        return
    events = parse_session_jsonl(transcript.read_text(encoding="utf-8", errors="replace"))
    results = replay(events)
    rendered = render_report_json(results) if output_format == "json" else render_report(results)
    typer.echo(rendered)
    if any(not result.ok for result in results):
        sys.exit(1)
