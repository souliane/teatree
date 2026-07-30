"""``--surface`` filtering for ``t3 eval run`` — slice the catalog by question surface.

The sibling of :mod:`teatree.cli.eval.lane_filter`, on the orthogonal axis: ``lane``
selects the harness MODE (clean-room vs under-load), ``surface`` selects which
question/answer SURFACE the scenario grades. ``--surface headless`` is how a run
excludes the advisory interactive scenarios outright rather than merely not gating on
them — the shape a headless-only CI leg wants.

``--surface`` is absent by default → no filtering → every existing run is unchanged.
"""

import typer

from teatree.eval.models import PERMITTED_SURFACES, EvalSpec


def filter_specs_by_surface(specs: list[EvalSpec], surface: str | None) -> list[EvalSpec]:
    """Return the specs on *surface*, or all specs when *surface* is ``None``.

    A non-``None`` surface outside :data:`~teatree.eval.models.PERMITTED_SURFACES` exits 2
    (CLI usage error) naming the known surfaces, rather than returning an empty,
    silently-green subset.
    """
    if surface is None:
        return specs
    if surface not in PERMITTED_SURFACES:
        permitted = ", ".join(sorted(PERMITTED_SURFACES))
        typer.echo(f"unknown --surface {surface!r}; known surfaces: {permitted}", err=True)
        raise typer.Exit(code=2)
    return [spec for spec in specs if spec.surface == surface]
