"""``t3 mutation`` — scoped (narrow) mutation testing over high-value safety modules.

``t3 mutation run`` diffs the PR's changed files against ``origin/main``,
intersects them with ``[tool.teatree.mutation].high_value_modules``, and mutates
ONLY the touched safety modules. When the intersection is empty it no-ops, so a
PR that touches no safety module pays nothing.

The exit code is the programmatic ratchet in
:meth:`teatree.quality.mutation_run.BaselineRatchet.verdict`: a run that surfaces
MORE surviving mutants than the recorded ``baseline_surviving`` total FAILS — in
both ``warn`` and ``block`` mode — because the surviving count may only ever
shrink. This is the prerequisite to flipping ``mode`` to ``"block"`` later.

``t3 mutation run --update-baseline`` rewrites the per-module
``baseline_surviving`` counts to the current measurement. The ratchet only moves
in the improving direction: an update that would record MORE survivors than the
committed baseline is REFUSED unless ``--allow-regression`` is also passed.
"""

from pathlib import Path

import typer
from rich.console import Console

from teatree.quality.mutation import registry_pyproject_path
from teatree.quality.mutation_run import (
    BaselineRatchet,
    MutationOutcome,
    MutationToolCrashError,
    load_baseline_per_module,
    load_settings,
    run_scoped,
)

mutation_app = typer.Typer(no_args_is_help=True, help="Scoped mutation testing over high-value safety modules.")
_console = Console()


def _report(outcome: MutationOutcome, *, baseline: int) -> None:
    if outcome.is_no_op:
        _console.print("[green]No high-value safety module in the diff — mutation run is a no-op.[/green]")
        return
    _console.print(f"[bold]Scoped modules:[/bold] {', '.join(outcome.scoped_modules)}")
    _console.print(f"  killed:       {len(outcome.killed)}")
    _console.print(f"  survived:     {len(outcome.survived)} (baseline {baseline})")
    _console.print(f"  inconclusive: {len(outcome.inconclusive)}")
    if outcome.survived:
        exceeds = BaselineRatchet.exceeds_baseline(outcome, baseline=baseline)
        colour = "red" if exceeds else "yellow"
        _console.print(f"[{colour}]Surviving mutants (tests do not catch these):[/{colour}]")
        for name in outcome.survived:
            _console.print(f"    {name}")
        if exceeds:
            _console.print(
                f"[red]Surviving count {len(outcome.survived)} exceeds the baseline {baseline} — "
                "add an assertion that kills a survivor, or, if this is an intentional reviewed "
                "increase, run `t3 mutation run --all --update-baseline --allow-regression`.[/red]"
            )
        elif len(outcome.survived) < baseline:
            _console.print(
                f"[yellow]Surviving count {len(outcome.survived)} is below the baseline {baseline} — "
                "tighten it with `t3 mutation run --all --update-baseline`.[/yellow]"
            )


@mutation_app.command()
def run(
    *,
    target: str = typer.Option("origin/main", "--target", help="Base ref to diff against"),
    all_modules: bool = typer.Option(False, "--all", help="Mutate the whole registry, not just the diff (weekly)"),
    update_baseline: bool = typer.Option(
        False,
        "--update-baseline",
        help="Rewrite the committed baseline_surviving counts to the current run (only shrinks).",
    ),
    allow_regression: bool = typer.Option(
        False,
        "--allow-regression",
        help="With --update-baseline, permit recording MORE survivors than committed "
        "(an intentional, reviewed increase). Refused by default so the ratchet cannot loosen.",
    ),
) -> None:
    """Mutate the safety modules a PR touches; fail when survivors exceed the baseline."""
    settings = load_settings()
    try:
        outcome = run_scoped(target=target, all_modules=all_modules)
    except MutationToolCrashError as exc:
        # Warn-first: a mutmut tool crash (timeout / process failure) is an
        # environment artifact, not a test gap. The full-scope mutation job is
        # advisory (NOT a branch-protection required check), so a crash must
        # print a WARNING and exit 0 — it can never block a merge. A genuine
        # survivors-over-baseline regression is reached below and still exits 1.
        _console.print(f"[yellow]WARNING: {exc} Treating the run as inconclusive and passing (warn-first).[/yellow]")
        return
    _report(outcome, baseline=settings.baseline_total)

    if update_baseline:
        _update_baseline(outcome, allow_regression=allow_regression)
        return

    code = BaselineRatchet.verdict(outcome, mode=settings.mode, baseline=settings.baseline_total)
    if code != 0:
        raise typer.Exit(code=code)


def _update_baseline(outcome: MutationOutcome, *, allow_regression: bool) -> None:
    if outcome.is_no_op:
        _console.print("[yellow]No safety module in scope — nothing to re-baseline.[/yellow]")
        return
    pyproject = registry_pyproject_path()
    committed = load_baseline_per_module(pyproject)
    new_baseline, loosens = BaselineRatchet.per_module(outcome, committed=committed)
    if loosens and not allow_regression:
        _console.print(
            "[red]Refusing to loosen the mutation baseline: this run surfaced more survivors than "
            "the committed baseline. The ratchet only moves in the improving direction. Kill the new "
            "survivors, or pass --allow-regression to record an intentional, reviewed increase.[/red]"
        )
        raise typer.Exit(code=1)
    if loosens:
        new_baseline = {**new_baseline, **BaselineRatchet.survivors_per_module(outcome)}
    _write_baseline(pyproject, new_baseline)
    direction = "loosened (regression allowed)" if loosens else "ratcheted"
    _console.print(f"[green]Baseline {direction}:[/green] {new_baseline}")


def _write_baseline(pyproject: Path, baseline: dict[str, int]) -> None:
    import tomlkit  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    doc = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    table = doc.setdefault("tool", {}).setdefault("teatree", {}).setdefault("mutation", tomlkit.table())
    array = tomlkit.array()
    array.multiline(multiline=True)
    for path, count in baseline.items():
        if count <= 0:
            continue
        entry = tomlkit.inline_table()
        entry["path"] = path
        entry["count"] = count
        array.append(entry)
    table["baseline_surviving"] = array
    pyproject.write_text(tomlkit.dumps(doc), encoding="utf-8")
