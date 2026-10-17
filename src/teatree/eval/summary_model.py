"""Shared model label for publish-safe eval summaries."""

from collections.abc import Sequence

from teatree.eval.discovery import find_spec
from teatree.eval.models import CachedPass
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import ScenarioResult


def summary_model(results: Sequence[ScenarioResult | PassAtKResult], cached: Sequence[CachedPass] = ()) -> str:
    for result in results:
        if isinstance(result, ScenarioResult):
            return result.spec.model
        spec = find_spec(result.spec_name)
        if spec is not None:
            return spec.model
    return cached[0].model if cached else "unknown"
