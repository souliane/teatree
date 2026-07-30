"""``t3 eval capture-subagent`` — copy a dispatched sub-agent JSONL to a scenario path."""

from pathlib import Path

import typer

from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.persistence import current_git_sha
from teatree.eval.subagent_capture import CaptureProvenance, capture_to
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
        help="Where to write <scenario>.jsonl (default: cwd) — must match `prepare-transcript`.",
    ),
    since: float = typer.Option(
        ...,
        "--since",
        help=(
            "REQUIRED. Only consider sub-agent JSONLs modified at/after this epoch. Record it BEFORE "
            "dispatching the scenario's Agent so a concurrent unrelated sub-agent (this is a 24/7-loop "
            "host) can never be grabbed as this scenario's transcript."
        ),
    ),
) -> None:
    """Copy the freshest in-session sub-agent JSONL to a scenario's transcript path.

    After the ``/t3:running-evals`` skill dispatches an ``Agent`` sub-agent for a
    scenario, Claude Code writes that sub-agent's trajectory under
    ``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl``. This
    command finds the newest such file written at/after the REQUIRED ``--since``
    epoch, validates it is a sub-agent transcript, copies it to
    ``<transcript_dir>/<scenario>.jsonl``, and writes a provenance sidecar (the
    scenario, its prompt hash, the repo HEAD SHA, the capture epoch) so ``t3 eval
    run --backend transcript`` grades it — $0 extra — AND refuses it if it is stale
    or belongs to a different scenario. Record the epoch BEFORE each dispatch and
    pass it as ``--since`` so back-to-back scenarios never grab a prior (or a
    concurrent unrelated) sub-agent's file.
    """
    ensure_django()
    spec = _require_spec(name)
    target = (transcript_dir or Path.cwd()) / f"{spec.name}.jsonl"
    source = capture_to(
        target,
        since=since,
        provenance=CaptureProvenance(scenario=spec.name, prompt=spec.prompt, head_sha=current_git_sha()),
    )
    if source is None:
        typer.echo(
            f"no sub-agent transcript found for {spec.name!r} modified at/after {since}; "
            "dispatch an Agent sub-agent on its prompt first (see /t3:running-evals)",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"captured {source} -> {target}")
