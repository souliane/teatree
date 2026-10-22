"""Single-trial eval persistence with cached scenario model labels."""

from typing import TYPE_CHECKING

from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult

if TYPE_CHECKING:
    from teatree.core.models import EvalRunRecord


def persist_single(
    results: list[ScenarioResult],
    *,
    specs: list[EvalSpec],
    max_turns: int | None,
    baseline: bool,
    cached_specs: list[EvalSpec] | None = None,
) -> "EvalRunRecord":
    from teatree.eval.persistence import persist_run  # noqa: PLC0415 — deferred: keeps CLI startup light

    all_specs = [*specs, *(cached_specs or [])]
    record = persist_run(
        results,
        model=",".join(sorted({spec.model for spec in all_specs})),
        max_turns_override=max_turns,
        cached_specs=cached_specs or (),
    )
    if baseline:
        record.mark_baseline()
    return record
