"""Refuse to publish a benchmark shard that is not backed by real metered spend.

The benchmark runs against a subscription OAuth window. When that window is
exhausted mid-run the remaining shards still "complete": the API calls return
without doing any metered work, every scenario force-FAILs, and the shard reports
``$0.0000`` for the affected model(s). Published unchecked, those all-red
zero-cost matrices land in the dashboard as if they were real results.

A shard is contaminated when ANY single model in its matrix has zero total cost
while that model has graded verdicts recorded — a filter keyed on ALL models
being zero-cost silently lets the partial case through (run 29760291395's
``under_load-3-5``: opus metered ``$0.4746`` with real verdicts while sonnet and
haiku were both ``$0.0000``, every scenario FAIL). A shard with no verdicts at
all owed no spend and is legitimately empty, not contaminated.

The refusal is whole-dashboard: one contaminated shard fails the publish rather
than being dropped from an otherwise-complete-looking commit, because a
partially-published dashboard is exactly the failure this guard prevents.
"""

import dataclasses
from pathlib import Path

from teatree.eval.matrix_html_tally import parse_model_tallies

SHARD_GLOB = "eval-benchmark-*.html"
_SHARD_PREFIX = "eval-benchmark-"


class UnmeteredShardError(RuntimeError):
    """One or more shards report graded verdicts against zero metered spend."""


@dataclasses.dataclass(frozen=True)
class ContaminatedShard:
    """One shard's zero-cost models, in the matrix's own column order."""

    shard: str
    path: Path
    zero_cost_models: tuple[str, ...]

    @property
    def summary(self) -> str:
        return f"{self.shard}: zero metered cost with recorded verdicts for {', '.join(self.zero_cost_models)}"


def contaminated_shards(dashboard_dir: Path) -> list[ContaminatedShard]:
    """Every shard artifact in *dashboard_dir* whose matrix is not backed by metered spend."""
    found = []
    for path in sorted(dashboard_dir.glob(SHARD_GLOB)):
        tallies = parse_model_tallies(path.read_text(encoding="utf-8"))
        zero_cost = tuple(tally.model for tally in tallies if tally.is_unmetered)
        if zero_cost:
            found.append(ContaminatedShard(shard=_shard_name(path), path=path, zero_cost_models=zero_cost))
    return found


def verify_publishable(dashboard_dir: Path) -> None:
    """Raise :class:`UnmeteredShardError` when any shard in *dashboard_dir* is contaminated."""
    shards = contaminated_shards(dashboard_dir)
    if not shards:
        return
    detail = "\n".join(f"  - {shard.summary}" for shard in shards)
    msg = (
        f"refusing to publish {len(shards)} unmetered benchmark shard(s) — the subscription "
        f"OAuth window was exhausted mid-run and these results are not real:\n{detail}\n"
        "Re-dispatch the run once the window has reset; do not publish this dashboard."
    )
    raise UnmeteredShardError(msg)


def _shard_name(path: Path) -> str:
    return path.stem.removeprefix(_SHARD_PREFIX)
