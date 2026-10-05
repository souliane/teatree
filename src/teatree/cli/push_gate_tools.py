"""``t3 tool push-gate`` — plan (or run) the safety-biased scoped push gate (#122).

Registers onto the shared ``tool_app`` (side-effect import from ``cli/__init__``,
mirroring ``comment_density_tools`` / ``test_path_mirror_tools``). The planning +
execution live in :mod:`teatree.quality.push_gate`; this module is the thin CLI
surface. It plans the diff and either prints the plan (``--json`` / ``--emit-cmd`` / default
human report) or executes the two scoped sweeps (``--run``, the driver
``dev/push-gate.sh`` invokes).
"""

import json
from pathlib import Path

import typer

from teatree.quality.push_gate import PushGatePlan, resolve_plan, run_push_gate, sweep_command


def _emit_cmd(plan: PushGatePlan, repo_root: Path) -> str:
    scope = "WHOLE-TREE" if plan.astgrep_scope is None else (" ".join(str(p) for p in plan.astgrep_scope) or "(none)")
    # A target-less plan runs no sweep at all. Emitting the bare command would hand
    # the reader something that DOES run — pytest would fall back to its own testpaths.
    invocation = (
        " ".join(sweep_command(repo_root, plan.doctest_targets))
        if plan.doctest_targets
        else "doctest sweep: (none — no changed module to sweep)"
    )
    return f"{invocation}\nast-grep scope: {scope}"


def _plan_as_dict(plan: PushGatePlan) -> dict:
    return {
        "is_full": plan.is_full,
        "reason": plan.reason,
        "doctest_targets": [str(t) for t in plan.doctest_targets],
        "astgrep_scope": None if plan.astgrep_scope is None else [str(p) for p in plan.astgrep_scope],
    }


def push_gate_command(
    base: str = typer.Option("origin/main", "--base", help="Merge-base ref for the changed set."),
    *,
    output_json: bool = typer.Option(False, "--json", help="Emit the machine-readable plan."),
    emit_cmd: bool = typer.Option(False, "--emit-cmd", help="Print the scoped doctest command + ast-grep scope."),
    run: bool = typer.Option(False, "--run", help="Execute the two scoped sweeps and exit non-zero on failure."),
) -> None:
    """Plan (or ``--run``) the incremental push gate: scoped doctest + ast-grep, FULL-fallback.

    A provably local diff is scoped; every uncertainty runs the whole sweep.
    The CI whole-tree backstop remains in place.
    """
    cwd = Path.cwd()
    plan = resolve_plan(base, cwd=cwd)

    if output_json:
        typer.echo(json.dumps(_plan_as_dict(plan), indent=2))
        return
    if emit_cmd:
        typer.echo(_emit_cmd(plan, cwd))
        return
    if run:
        result = run_push_gate(plan, repo_root=cwd)
        for note in result.notes:
            typer.echo(note)
        if result.astgrep_findings:
            typer.echo(f"ast-grep findings ({len(result.astgrep_findings)}):")
            for finding in result.astgrep_findings:
                typer.echo(f"  {finding['check_id']}  {finding['path']}:{finding['start']['line']}")
        raise typer.Exit(code=result.exit_code)

    typer.echo(plan.report())
    typer.echo(f"reason: {plan.reason}")


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("push-gate")(push_gate_command)
