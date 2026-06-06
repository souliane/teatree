"""``t3 eval capture-subagent`` — copy a dispatched sub-agent JSONL to a scenario path."""

from pathlib import Path

import typer

from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.subagent_capture import capture_to
from teatree.utils.django_bootstrap import ensure_django


def _require_spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    if spec is None:
        typer.echo(f"unknown scenario: {name!r}", err=True)
        available = ", ".join(s.name for s in discover_specs()) or "(none)"
        typer.echo(f"available scenarios: {available}", err=True)
        raise typer.Exit(code=2)
    return spec


def capture_subagent(
    name: str = typer.Argument(..., help="Scenario name whose transcript to capture."),
    transcript_dir: Path | None = typer.Option(
        None,
        "--transcript-dir",
        help="Where to write <scenario>.jsonl (default: cwd) — must match `prepare-subscription`.",
    ),
    since: float | None = typer.Option(
        None,
        "--since",
        help="Only consider sub-agent JSONLs modified at/after this epoch (disambiguates sequential dispatches).",
    ),
) -> None:
    """Copy the freshest in-session sub-agent JSONL to a scenario's transcript path.

    After the ``/t3:running-evals`` skill dispatches an ``Agent`` sub-agent for a
    scenario, Claude Code writes that sub-agent's trajectory under
    ``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl``. This
    command finds the newest such file (optionally one written at/after
    ``--since``), validates it is a sub-agent transcript, and copies it to
    ``<transcript_dir>/<scenario>.jsonl`` so ``t3 eval run --backend
    subscription`` grades it — no ``claude -p`` spend. Record an epoch BEFORE each
    dispatch and pass it as ``--since`` so back-to-back scenarios never grab a
    prior sub-agent's file.
    """
    ensure_django()
    spec = _require_spec(name)
    target = (transcript_dir or Path.cwd()) / f"{spec.name}.jsonl"
    source = capture_to(target, since=since)
    if source is None:
        typer.echo(
            f"no sub-agent transcript found for {spec.name!r}"
            + (f" modified at/after {since}" if since is not None else "")
            + "; dispatch an Agent sub-agent on its prompt first (see /t3:running-evals)",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"captured {source} -> {target}")
