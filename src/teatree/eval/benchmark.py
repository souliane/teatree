"""Per-variant benchmark summary: the ``t3 eval benchmark`` value object and renderers.

A benchmark run executes the scenario suite once per ``model@effort`` variant
(via the model-matrix machinery) and folds the resulting
:class:`~teatree.eval.matrix.MatrixRow` cells into one :class:`VariantSummary`
per variant: scenarios passed/executed, pass-rate, total metered cost, mean
cost per scenario, and cost per pass — the cost/quality comparison the lane
exists to answer (e.g. opus@xhigh vs sonnet@medium). Summaries and renderers
are pure (rows in, string out), so they are unit-testable without the SDK or
the DB.
"""

import dataclasses
import json

from teatree.eval.cost_fit import CostCell, warm_equivalent_cost
from teatree.eval.matrix import MatrixRow
from teatree.eval.models import CAP_TERMINAL_REASONS, TokenUsage


@dataclasses.dataclass(frozen=True)
class VariantSummary:
    """One variant's aggregated benchmark metrics across the scenario suite."""

    variant: str
    passed: int
    #: Cells that actually ran and were graded — the pass-rate/mean-cost
    #: denominator. EXCLUDES both skipped (not provisioned) and errored (the
    #: runner raised even after retries), so a transient infra blip never
    #: unfairly lowers a variant's measured pass-rate.
    executed: int
    skipped: int
    errored: int
    total_cost_usd: float
    #: Metered cost summed across the executed cells, split into the requested
    #: MAIN model (the headline comparison number) and the AUXILIARY background
    #: (Claude Code's ``claude-haiku-4-5``). ``main + aux`` need NOT equal
    #: ``total_cost_usd`` exactly — the API's billed total can carry rounding /
    #: per-call fees the per-model split doesn't — so the billed total stays the
    #: headline and the split is the observability around it.
    main_cost_usd: float = 0.0
    aux_cost_usd: float = 0.0
    #: Token usage summed across the executed cells (errored/skipped excluded) —
    #: the substrate for the token-weighted cache columns below.
    usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    #: Count of executed cells whose billed model fell back to a different model.
    fell_back_cells: int = 0
    #: The bounded "warm-equivalent" cost: what the variant would pay if every
    #: cell fully benefited from the cache. ``None`` when the per-variant fit
    #: degrades (too few clean cells / ill-conditioned) — never fabricated.
    warm_equivalent_cost_usd: float | None = None

    @property
    def pass_rate(self) -> float:
        return self.passed / self.executed if self.executed else 0.0

    @property
    def mean_cost_usd(self) -> float:
        return self.total_cost_usd / self.executed if self.executed else 0.0

    @property
    def cost_per_pass_usd(self) -> float | None:
        """Total cost divided by passes — ``None`` when nothing passed (undefined)."""
        return self.total_cost_usd / self.passed if self.passed else None

    @property
    def cache_hit_rate(self) -> float:
        """Token-weighted cache-hit rate (sum-then-divide, NOT a mean of per-cell ratios)."""
        return self.usage.cache_hit_rate

    @property
    def cold_write_fraction(self) -> float:
        """Share of input that did NOT benefit from cache (``cold_write_tokens / total_input``); 0.0 when no input."""
        total = self.usage.total_input
        return self.usage.cold_write_tokens / total if total else 0.0

    @property
    def mean_output_tokens(self) -> float:
        """Mean output tokens per executed cell — the model-attributable cost axis; 0.0 when none executed."""
        return self.usage.output / self.executed if self.executed else 0.0

    @property
    def aux_cost_fraction(self) -> float:
        """Share of metered cost spent on the AUXILIARY background; 0.0 when no metered cost.

        The reader's "how much of the run is haiku vs the requested model" number.
        Denominator is the billed ``total_cost_usd`` (the real spend), so a value
        near 0 means the requested model dominates.
        """
        return self.aux_cost_usd / self.total_cost_usd if self.total_cost_usd else 0.0


def summarize_benchmark(rows: list[MatrixRow], variants: list[str]) -> list[VariantSummary]:
    """Fold matrix rows into one summary per variant, in the given variant order."""
    summaries: list[VariantSummary] = []
    for variant in variants:
        cells = [row for row in rows if row.model == variant]
        # "executed" here is the pass-rate denominator: graded cells only, so it
        # excludes BOTH skipped and errored. (Distinct from RunGuards.executed in
        # run_modes.py, which counts ``not skipped`` — an errored cell still
        # proves the suite ran something there.)
        executed = [cell for cell in cells if not cell.skipped and not cell.errored]
        usage = sum((cell.usage for cell in executed), TokenUsage())
        summaries.append(
            VariantSummary(
                variant=variant,
                passed=sum(1 for cell in executed if cell.passed),
                executed=len(executed),
                skipped=sum(1 for cell in cells if cell.skipped),
                errored=sum(1 for cell in cells if cell.errored),
                total_cost_usd=sum(cell.cost_usd for cell in executed),
                main_cost_usd=sum(cell.main_cost_usd for cell in executed),
                aux_cost_usd=sum(cell.aux_cost_usd for cell in executed),
                usage=usage,
                fell_back_cells=sum(1 for cell in executed if cell.fell_back),
                warm_equivalent_cost_usd=warm_equivalent_cost(_clean_cost_cells(executed)),
            )
        )
    return summaries


def _clean_cost_cells(executed: list[MatrixRow]) -> list[CostCell]:
    """The executed cells whose billed cost matches the clean identity — the fit's input.

    EXCLUDES cap-truncated cells (a cap terminal reason), fallback cells (billed
    cost mixes model rates), and zero-cost cells (a non-metered/subscription
    row carries no usable billed number). Errored/skipped cells are already
    excluded upstream (they are not in ``executed``).
    """
    return [
        CostCell(usage=cell.usage, billed_usd=cell.cost_usd)
        for cell in executed
        if cell.cost_usd > 0.0 and not cell.fell_back and cell.terminal_reason not in CAP_TERMINAL_REASONS
    ]


def render_benchmark_text(summaries: list[VariantSummary]) -> str:
    """Render the per-variant comparison table (one line per variant).

    Billed ``total cost`` stays the headline; ``main cost`` / ``aux cost`` /
    ``aux%`` split it into the requested model vs Claude Code's haiku background,
    and the ``cache-hit%`` / ``cold-write%`` / ``mean-out-tok`` / ``warm-cost``
    columns are the honest cache-cost diagnostics. ``warm-cost`` is ``-`` when the
    per-variant fit degrades. Any variant with a fallen-back cell appends a
    clearly-visible note.
    """
    headers = (
        "variant",
        "passed",
        "pass-rate",
        "errored",
        "total cost",
        "main cost",
        "aux cost",
        "aux%",
        "mean cost/scn",
        "cost/pass",
        "cache-hit%",
        "cold-write%",
        "mean-out-tok",
        "warm-cost",
    )
    rows = [
        (
            summary.variant,
            f"{summary.passed}/{summary.executed}",
            f"{summary.pass_rate:.2f}",
            str(summary.errored),
            f"${summary.total_cost_usd:.4f}",
            f"${summary.main_cost_usd:.4f}",
            f"${summary.aux_cost_usd:.4f}",
            f"{summary.aux_cost_fraction:.0%}",
            f"${summary.mean_cost_usd:.4f}",
            "-" if summary.cost_per_pass_usd is None else f"${summary.cost_per_pass_usd:.4f}",
            f"{summary.cache_hit_rate:.0%}",
            f"{summary.cold_write_fraction:.0%}",
            f"{summary.mean_output_tokens:.0f}",
            "-" if summary.warm_equivalent_cost_usd is None else f"${summary.warm_equivalent_cost_usd:.4f}",
        )
        for summary in summaries
    ]
    widths = [max([len(header), *(len(row[i]) for row in rows)]) for i, header in enumerate(headers)]
    header_line = "  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True))
    lines = [header_line, "-" * len(header_line)]
    lines.extend("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)) for row in rows)
    lines.extend(
        f"! {summary.variant}: {summary.fell_back_cells} cell(s) fell back to a different model "
        "— billed cost mixes model rates"
        for summary in summaries
        if summary.fell_back_cells > 0
    )
    return "\n".join(lines)


def render_benchmark_json(summaries: list[VariantSummary]) -> str:
    """Render ``{"variants": [{variant, passed, …, usage, cache metrics, warm cost}]}``."""
    payload = {
        "variants": [
            {
                "variant": summary.variant,
                "passed": summary.passed,
                "executed": summary.executed,
                "skipped": summary.skipped,
                "errored": summary.errored,
                "pass_rate": summary.pass_rate,
                "total_cost_usd": summary.total_cost_usd,
                "mean_cost_usd": summary.mean_cost_usd,
                "cost_per_pass_usd": summary.cost_per_pass_usd,
                "main_cost_usd": summary.main_cost_usd,
                "aux_cost_usd": summary.aux_cost_usd,
                "aux_cost_fraction": summary.aux_cost_fraction,
                "usage": {
                    "input": summary.usage.input,
                    "cache_creation": summary.usage.cache_creation,
                    "cache_read": summary.usage.cache_read,
                    "output": summary.usage.output,
                },
                "cache_hit_rate": summary.cache_hit_rate,
                "cold_write_fraction": summary.cold_write_fraction,
                "mean_output_tokens": summary.mean_output_tokens,
                "warm_equivalent_cost_usd": summary.warm_equivalent_cost_usd,
                "fell_back_cells": summary.fell_back_cells,
            }
            for summary in summaries
        ]
    }
    return json.dumps(payload, indent=2)
