"""The committed pre-Opus-5 baseline: the loader, and the diff against a live aggregate.

The baseline is a FILE, not a query. A table holding both pre- and post-cutover
attempts separates them by nothing it records — ``model`` alone does not, because
a phase is free to serve more than one model inside a single window. So the
pre-cutover distribution is pinned as data, and every change to it is reviewed in
a diff (the same reason ``evals/cost_bounds.yaml`` is checked in rather than
derived).

A malformed baseline raises. A silently-empty one would make every post-cutover
comparison vacuously green, which is the failure this file exists to prevent.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from teatree.core.cost_baseline.aggregate import UsageStats

#: The committed freeze, resolved from this module so the package stays relocatable.
FROZEN_BASELINE_PATH = Path(__file__).resolve().parent / "pre_opus5.yaml"

#: The one :class:`UsageStats` metric that is a count rather than a measurement.
_INT_METRICS = frozenset({"attempts"})


class BaselineError(ValueError):
    """A malformed or absent baseline file — a typo'd metric, a truncated table."""


@dataclass(frozen=True, slots=True)
class FrozenBaseline:
    """One frozen measurement window, with the provenance needed to trust it."""

    cutover_model: str
    cutover_pull_request: int
    cutover_merged_at: str
    first_attempt_started_at: str
    last_attempt_started_at: str
    taskattempt_rows_total: int
    real_dispatch_rows: int
    rows_with_cost_usd: int
    rows_with_output_tokens: int
    post_cutover_rows: int
    per_phase: dict[str, UsageStats]
    per_phase_model: dict[tuple[str, str], UsageStats]

    @property
    def predicates_agree(self) -> bool:
        """Whether the three candidate real-dispatch predicates select the same rows.

        ``output_tokens IS NOT NULL``, ``cost_usd IS NOT NULL``, and "carries a
        model" are three different filters that happen to coincide on this window.
        Recording the agreement is what makes ``output_tokens IS NOT NULL`` a
        justified choice rather than an assumed one.
        """
        return self.real_dispatch_rows == self.rows_with_cost_usd == self.rows_with_output_tokens


@dataclass(frozen=True, slots=True)
class PhaseComparison:
    """One phase's live distribution against its frozen counterpart.

    Ratios are ``live / frozen``, so ``12.0`` reads as the twelve-fold median
    token growth a model upgrade is capable of. ``baselined`` is ``False`` for a
    phase the freeze never observed, and every ratio is then ``None`` — an
    unmeasurable comparison is reported as unmeasurable, never as ``1.0``.
    """

    phase: str
    baselined: bool
    live: UsageStats
    frozen: UsageStats | None
    median_output_tokens_ratio: float | None
    p95_output_tokens_ratio: float | None
    median_billed_input_tokens_ratio: float | None
    median_num_turns_ratio: float | None
    median_cost_usd_ratio: float | None


def load_frozen_baseline(path: Path | None = None) -> FrozenBaseline:
    """Parse the committed freeze into a typed :class:`FrozenBaseline`."""
    baseline_path = path or FROZEN_BASELINE_PATH
    if not baseline_path.is_file():
        msg = f"cost baseline file is missing: {baseline_path}. Without it no cutover comparison is possible."
        raise BaselineError(msg)
    loaded = yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        msg = f"{baseline_path}: expected a top-level mapping"
        raise BaselineError(msg)
    cutover = _section(loaded, "cutover", baseline_path)
    window = _section(loaded, "window", baseline_path)
    coverage = _section(loaded, "coverage", baseline_path)
    return FrozenBaseline(
        cutover_model=_required_str(cutover, "model", baseline_path),
        cutover_pull_request=_required_int(cutover, "pull_request", baseline_path),
        cutover_merged_at=_required_str(cutover, "merged_at", baseline_path),
        first_attempt_started_at=_required_str(window, "first_attempt_started_at", baseline_path),
        last_attempt_started_at=_required_str(window, "last_attempt_started_at", baseline_path),
        taskattempt_rows_total=_required_int(coverage, "taskattempt_rows_total", baseline_path),
        real_dispatch_rows=_required_int(coverage, "real_dispatch_rows", baseline_path),
        rows_with_cost_usd=_required_int(coverage, "rows_with_cost_usd", baseline_path),
        rows_with_output_tokens=_required_int(coverage, "rows_with_output_tokens", baseline_path),
        post_cutover_rows=_required_int(coverage, "post_cutover_rows", baseline_path),
        per_phase=_parse_per_phase(loaded, baseline_path),
        per_phase_model=_parse_per_phase_model(loaded, baseline_path),
    )


def compare_phases(live: Mapping[str, UsageStats], baseline: FrozenBaseline) -> list[PhaseComparison]:
    """Diff a LIVE per-phase aggregate against the freeze, one row per live phase.

    Driven by the live side: a frozen phase with no live attempts is omitted
    rather than reported as a collapse to zero, and a live phase the freeze never
    saw is reported unbaselined rather than dropped.
    """
    comparisons: list[PhaseComparison] = []
    for phase in sorted(live):
        live_stats = live[phase]
        frozen = baseline.per_phase.get(phase)
        comparisons.append(
            PhaseComparison(
                phase=phase,
                baselined=frozen is not None,
                live=live_stats,
                frozen=frozen,
                median_output_tokens_ratio=_ratio(live_stats.median_output_tokens, frozen, "median_output_tokens"),
                p95_output_tokens_ratio=_ratio(live_stats.p95_output_tokens, frozen, "p95_output_tokens"),
                median_billed_input_tokens_ratio=_ratio(
                    live_stats.median_billed_input_tokens, frozen, "median_billed_input_tokens"
                ),
                median_num_turns_ratio=_ratio(live_stats.median_num_turns, frozen, "median_num_turns"),
                median_cost_usd_ratio=_ratio(live_stats.median_cost_usd, frozen, "median_cost_usd"),
            )
        )
    return comparisons


def _ratio(live_value: float, frozen: UsageStats | None, metric: str) -> float | None:
    if frozen is None:
        return None
    frozen_value = float(getattr(frozen, metric))
    if frozen_value <= 0.0:
        return None
    return live_value / frozen_value


def _section(document: Mapping[str, Any], name: str, path: Path) -> Mapping[str, Any]:
    section = document.get(name)
    if not isinstance(section, Mapping):
        msg = f"{path}: '{name}' must be a mapping"
        raise BaselineError(msg)
    return section


def _required(section: Mapping[str, Any], key: str, path: Path) -> object:
    if key not in section:
        msg = f"{path}: missing required key '{key}'"
        raise BaselineError(msg)
    return section[key]


def _required_str(section: Mapping[str, Any], key: str, path: Path) -> str:
    value = _required(section, key, path)
    if not isinstance(value, str):
        msg = f"{path}: '{key}' must be a string, got {value!r}"
        raise BaselineError(msg)
    return value


def _required_int(section: Mapping[str, Any], key: str, path: Path) -> int:
    value = _required(section, key, path)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{path}: '{key}' must be an integer, got {value!r}"
        raise BaselineError(msg)
    return value


def _metric(raw: Mapping[str, Any], name: str, *, label: str, path: Path) -> float:
    if name not in raw:
        msg = f"{path}: {label} is missing required metric '{name}'"
        raise BaselineError(msg)
    value = raw[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{path}: {label}.{name} must be a number, got {value!r}"
        raise BaselineError(msg)
    return float(value)


def _usage_stats(raw: object, *, label: str, path: Path) -> UsageStats:
    if not isinstance(raw, Mapping):
        msg = f"{path}: {label} must map to the usage-stats metrics"
        raise BaselineError(msg)
    keyed: dict[str, Any] = {str(key): value for key, value in raw.items()}
    metric = {f.name: _metric(keyed, f.name, label=label, path=path) for f in fields(UsageStats)}
    return UsageStats(
        attempts=int(metric["attempts"]),
        median_output_tokens=metric["median_output_tokens"],
        p95_output_tokens=metric["p95_output_tokens"],
        median_billed_input_tokens=metric["median_billed_input_tokens"],
        p95_billed_input_tokens=metric["p95_billed_input_tokens"],
        median_num_turns=metric["median_num_turns"],
        p95_num_turns=metric["p95_num_turns"],
        median_cost_usd=metric["median_cost_usd"],
        p95_cost_usd=metric["p95_cost_usd"],
        total_cost_usd=metric["total_cost_usd"],
    )


def _parse_per_phase(document: Mapping[str, Any], path: Path) -> dict[str, UsageStats]:
    section = _section(document, "per_phase", path)
    return {str(phase): _usage_stats(raw, label=f"per_phase.{phase}", path=path) for phase, raw in section.items()}


def _parse_per_phase_model(document: Mapping[str, Any], path: Path) -> dict[tuple[str, str], UsageStats]:
    section = _section(document, "per_phase_model", path)
    parsed: dict[tuple[str, str], UsageStats] = {}
    for phase, models in section.items():
        if not isinstance(models, Mapping):
            msg = f"{path}: per_phase_model.{phase} must map model ids to metrics"
            raise BaselineError(msg)
        for model, raw in models.items():
            parsed[str(phase), str(model)] = _usage_stats(raw, label=f"per_phase_model.{phase}.{model}", path=path)
    return parsed
