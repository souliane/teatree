"""``t3 mutation`` — scoped (narrow) mutation testing over high-value safety modules.

``t3 mutation run`` diffs the PR's changed files against ``origin/main``,
intersects them with ``[tool.teatree.mutation].high_value_modules``, and mutates
ONLY the touched safety modules. When the intersection is empty it no-ops, so a
PR that touches no safety module pays nothing. The exit code follows the
warn/block ratchet in :func:`teatree.quality.mutation_run.decide_verdict` — warn
mode never fails the pipeline; block mode fails only on a survivor count above
the documented baseline.
"""

import typer
from rich.console import Console

from teatree.quality.mutation_run import MutationOutcome, decide_verdict, load_settings, run_scoped

mutation_app = typer.Typer(no_args_is_help=True, help="Scoped mutation testing over high-value safety modules.")
_console = Console()


def _report(outcome: MutationOutcome, *, mode: str) -> None:
    if outcome.is_no_op:
        _console.print("[green]No high-value safety module in the diff — mutation run is a no-op.[/green]")
        return
    _console.print(f"[bold]Scoped modules:[/bold] {', '.join(outcome.scoped_modules)}")
    _console.print(f"  killed:       {len(outcome.killed)}")
    _console.print(f"  survived:     {len(outcome.survived)}")
    _console.print(f"  inconclusive: {len(outcome.inconclusive)}")
    if outcome.survived:
        colour = "yellow" if mode == "warn" else "red"
        _console.print(f"[{colour}]Surviving mutants (tests do not catch these):[/{colour}]")
        for name in outcome.survived:
            _console.print(f"    {name}")


@mutation_app.command()
def run(
    *,
    target: str = typer.Option("origin/main", "--target", help="Base ref to diff against"),
) -> None:
    """Mutate the safety modules a PR touches; warn/block per the ratchet."""
    settings = load_settings()
    outcome = run_scoped(target=target)
    _report(outcome, mode=settings.mode)
    code = decide_verdict(outcome, mode=settings.mode, baseline=settings.baseline_total)
    if code != 0:
        raise typer.Exit(code=code)
