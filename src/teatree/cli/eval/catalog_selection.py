"""The single chokepoint for "which specs does this ``t3 eval run`` grade?".

Four independent selectors compose here — an explicit scenario ``name``, then
``--lane`` (harness mode), ``--surface`` (question surface) and ``--shard`` (a
deterministic partition). Composing them in one place keeps the CLI body a flag
surface rather than selection logic, and gives the ``--docker`` passthrough and the
workflows one function to agree with.
"""

import typer

from teatree.cli.eval.app_helpers import require_spec
from teatree.cli.eval.lane_filter import filter_specs_by_lane
from teatree.cli.eval.surface_filter import filter_specs_by_surface
from teatree.eval.lane_shard import ShardSpecError, filter_specs_by_shard, resolve_rotating_shard
from teatree.eval.models import EvalSpec


def select_specs(
    catalog: list[EvalSpec], name: str | None, *, lane: str | None, surface: str | None, shard: str | None
) -> list[EvalSpec]:
    """Resolve the run's spec set; a named scenario bypasses every catalog filter.

    *catalog* is passed in rather than discovered here so the CLI keeps ownership of
    discovery (and its test seam). A malformed ``--shard`` exits 2 (CLI usage error)
    rather than silently grading an empty, green subset. A date-rotated
    ``--shard rotate/<total>`` is announced with the shard it resolved to and the
    date arithmetic behind it, so the run states which slice of the catalog it
    graded and re-running that reported token replays the same subset.
    """
    if name is not None:
        return [require_spec(name)]
    specs = filter_specs_by_surface(filter_specs_by_lane(catalog, lane), surface)
    try:
        resolution = resolve_rotating_shard(shard)
        if resolution.reason:
            typer.echo(f"eval shard: {resolution.reason}")
        return filter_specs_by_shard(specs, resolution.token)
    except ShardSpecError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
