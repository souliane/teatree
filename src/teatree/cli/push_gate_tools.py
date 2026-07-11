"""``t3 tool push-gate`` — plan (or run) the safety-biased incremental push gate (#122).

Registers onto the shared ``tool_app`` (side-effect import from ``cli/__init__``,
mirroring ``comment_density_tools`` / ``test_path_mirror_tools``). The planning +
execution live in :mod:`teatree.quality.push_gate`; this module is the thin CLI
surface. It resolves the ``incremental_push_gate`` flag (fail-safe to OFF ⇒ FULL),
plans the diff, and either prints the plan (``--json`` / ``--emit-cmd`` / default
human report) or executes the two scoped sweeps (``--run``, the driver
``dev/push-gate.sh`` invokes).
"""

import json
import sys
from pathlib import Path

import typer

from teatree.config import get_effective_settings
from teatree.quality.push_gate import PushGatePlan, resolve_plan, run_push_gate
from teatree.utils.django_bootstrap import ensure_django


def _resolve_flag() -> bool:
    """Resolve ``incremental_push_gate``, failing SAFE to OFF (⇒ whole-tree FULL).

    The flag defaults ON, but any failure to bootstrap Django or read the store
    resolves it to OFF (fail-safe), so a broken/unconfigured environment runs the
    whole-tree sweeps — never a scoped run it could not authorize.
    """
    try:
        ensure_django()
        return bool(get_effective_settings().incremental_push_gate)
    except Exception:  # noqa: BLE001 — fail safe: any read failure ⇒ OFF ⇒ whole-tree FULL.
        return False


def _emit_cmd(plan: PushGatePlan) -> str:
    doctest = " ".join(str(t) for t in plan.doctest_targets) or "(none)"
    scope = "WHOLE-TREE" if plan.astgrep_scope is None else (" ".join(str(p) for p in plan.astgrep_scope) or "(none)")
    return f"{sys.executable} -m pytest --doctest-modules {doctest}\nast-grep scope: {scope}"


def _plan_as_dict(plan: PushGatePlan) -> dict:
    return {
        "is_full": plan.is_full,
        "reason": plan.reason,
        "enabled": plan.enabled,
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

    The ``incremental_push_gate`` flag defaults ON ⇒ scoped to the diff, with FULL as
    the classifier's default branch (every uncertainty runs the whole sweep). OFF ⇒
    whole-tree both sweeps (the pre-#122 behaviour). A read failure fails safe to
    whole-tree FULL, and the CI whole-tree backstop is untouched regardless of the flag.
    """
    enabled = _resolve_flag()
    cwd = Path.cwd()
    plan = resolve_plan(base, enabled=enabled, cwd=cwd)

    if output_json:
        typer.echo(json.dumps(_plan_as_dict(plan), indent=2))
        return
    if emit_cmd:
        typer.echo(_emit_cmd(plan))
        return
    if run:
        result = run_push_gate(plan, repo_root=cwd)
        for note in result.notes:
            typer.echo(note)
        if result.astgrep_findings:
            typer.echo(f"ast-grep findings ({len(result.astgrep_findings)}):")
            for finding in result.astgrep_findings:
                typer.echo(f"  {finding['check_id']}  {finding['path']}:{finding['start']['line']}")
        raise typer.Exit(code=0 if result.ok else 1)

    typer.echo(plan.report())
    typer.echo(f"reason: {plan.reason}")


def register(app: typer.Typer) -> None:
    """Register this module's ``t3 tool`` command(s) onto *app* (called from ``cli/__init__``)."""
    app.command("push-gate")(push_gate_command)
