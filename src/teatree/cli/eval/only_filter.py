"""``--only`` filtering for ``t3 eval run`` — restrict to a named scenario subset.

The selective-PR eval workflow passes ``--only "<comma names>"`` (the scenarios
the PR's diff touched, from ``scripts/eval/scenarios_for_changed.py``) so the run
meters EXACTLY those scenarios and nothing else. The filter composes with
``--lane``/``--shard``: those slice the catalog first, then ``--only`` further
restricts to the named subset.

Each requested name is validated against the WHOLE catalog (``discover_specs``),
so an unknown name fails loud (exit 2) naming the offender — never silently
dropped. A name that exists in the catalog but was already sliced out by
``--lane``/``--shard`` is legitimately absent from the result, not an error.
"""

import typer

from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec


def filter_specs_by_only(specs: list[EvalSpec], only: str | None) -> list[EvalSpec]:
    """Return the specs in *only*, or all specs when *only* is ``None``.

    ``None`` returns *specs* unchanged (the unfiltered default). A non-``None``
    value is parsed as a comma-separated name list; an empty list, or any name
    absent from the catalog, exits 2 (CLI usage error) rather than running an
    empty/partial subset. The returned specs keep *specs*' order (catalog order).
    """
    if only is None:
        return specs
    requested = [name.strip() for name in only.split(",") if name.strip()]
    if not requested:
        typer.echo("--only was empty; pass e.g. --only scenario_a,scenario_b", err=True)
        raise typer.Exit(code=2)
    catalog_names = {spec.name for spec in discover_specs()}
    unknown = [name for name in requested if name not in catalog_names]
    if unknown:
        typer.echo(f"unknown --only scenario(s): {', '.join(unknown)}", err=True)
        raise typer.Exit(code=2)
    wanted = set(requested)
    return [spec for spec in specs if spec.name in wanted]
