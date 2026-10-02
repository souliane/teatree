"""Transcript preparation command for the transcript eval backend."""

import json
from pathlib import Path

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.cli.eval.app_helpers import require_spec
from teatree.cli.eval.run_modes import build_transcript_manifest, render_transcript_text
from teatree.eval.discovery import discover_specs
from teatree.utils.django_bootstrap import ensure_django


def prepare_transcript(
    name: str | None = typer.Argument(None, help="Scenario name to prepare (omit to prepare all)."),
    transcript_dir: Path | None = typer.Option(
        None,
        "--transcript-dir",
        help="Where `t3 eval capture-subagent` writes each <scenario>.jsonl transcript (default: cwd).",
    ),
    output_format: str = typer.Option("text", "--format", help="Manifest format: text or json."),
) -> None:
    """Emit the per-scenario prompts for a LOCAL transcript-backend eval run.

    The eval CLI is a plain process with no in-session ``Agent`` tool, so it
    cannot itself drive a subscription-covered turn. This command prints, per
    scenario, the agent definition, prompt, and the transcript path the
    ``transcript`` backend will read.
    """
    ensure_django()
    require_valid_format(output_format)
    specs = discover_specs() if name is None else [require_spec(name)]
    manifest = build_transcript_manifest(specs, transcript_dir or Path.cwd())
    typer.echo(json.dumps(manifest, indent=2) if output_format == "json" else render_transcript_text(manifest))
