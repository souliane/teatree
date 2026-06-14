"""``--lane`` filtering for ``t3 eval run`` and the full suite.

``discover_specs()`` returns the WHOLE catalog regardless of lane; the cheap
PR-path anti-vacuity gate and the weekly metered lane read that same catalog but
run different subsets. :func:`filter_specs_by_lane` is the single chokepoint that
slices the catalog by :attr:`EvalSpec.lane`, so the CLI flag and the workflow
``--lane`` both resolve identically.

``--lane`` is absent by default → no filtering → every existing run is
unchanged. A value is validated against :data:`PERMITTED_LANES` (the loader's
own set) so an unknown lane fails loud rather than silently matching nothing.
"""

import typer

from teatree.eval.models import PERMITTED_LANES, EvalSpec


def filter_specs_by_lane(specs: list[EvalSpec], lane: str | None) -> list[EvalSpec]:
    """Return the specs in *lane*, or all specs when *lane* is ``None``.

    ``None`` (the unfiltered default) returns *specs* unchanged so the default
    behaviour of every command is preserved. A non-``None`` lane outside
    :data:`PERMITTED_LANES` exits 2 (CLI usage error) naming the known lanes,
    rather than returning an empty, silently-green subset.
    """
    if lane is None:
        return specs
    if lane not in PERMITTED_LANES:
        permitted = ", ".join(sorted(PERMITTED_LANES))
        typer.echo(f"unknown --lane {lane!r}; known lanes: {permitted}", err=True)
        raise typer.Exit(code=2)
    return [spec for spec in specs if spec.lane == lane]
