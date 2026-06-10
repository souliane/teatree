"""Per-variant benchmark summary: the ``t3 eval benchmark`` value object and renderers.

A benchmark run executes the scenario suite once per ``model@effort`` variant
(via the model-matrix machinery) and folds the resulting
:class:`~teatree.eval.matrix.MatrixRow` cells into one :class:`VariantSummary`
per variant: scenarios passed/executed, pass-rate, total metered cost, mean
cost per scenario, and cost per pass — the cost/quality comparison the lane
exists to answer (e.g. opus@xhigh vs fable@medium). Summaries and renderers
are pure (rows in, string out), so they are unit-testable without the SDK or
the DB.
"""

import dataclasses
import json

from teatree.eval.matrix import MatrixRow


@dataclasses.dataclass(frozen=True)
class VariantSummary:
    """One variant's aggregated benchmark metrics across the scenario suite."""

    variant: str
    passed: int
    executed: int
    skipped: int
    total_cost_usd: float

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


def summarize_benchmark(rows: list[MatrixRow], variants: list[str]) -> list[VariantSummary]:
    """Fold matrix rows into one summary per variant, in the given variant order."""
    summaries: list[VariantSummary] = []
    for variant in variants:
        cells = [row for row in rows if row.model == variant]
        executed = [cell for cell in cells if not cell.skipped]
        summaries.append(
            VariantSummary(
                variant=variant,
                passed=sum(1 for cell in executed if cell.passed),
                executed=len(executed),
                skipped=len(cells) - len(executed),
                total_cost_usd=sum(cell.cost_usd for cell in executed),
            )
        )
    return summaries


def render_benchmark_text(summaries: list[VariantSummary]) -> str:
    """Render the per-variant comparison table (one line per variant)."""
    headers = ("variant", "passed", "pass-rate", "total cost", "mean cost/scn", "cost/pass")
    rows = [
        (
            summary.variant,
            f"{summary.passed}/{summary.executed}",
            f"{summary.pass_rate:.2f}",
            f"${summary.total_cost_usd:.4f}",
            f"${summary.mean_cost_usd:.4f}",
            "-" if summary.cost_per_pass_usd is None else f"${summary.cost_per_pass_usd:.4f}",
        )
        for summary in summaries
    ]
    widths = [max([len(header), *(len(row[i]) for row in rows)]) for i, header in enumerate(headers)]
    header_line = "  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True))
    lines = [header_line, "-" * len(header_line)]
    lines.extend("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)) for row in rows)
    return "\n".join(lines)


def render_benchmark_json(summaries: list[VariantSummary]) -> str:
    """Render ``{"variants": [{variant, passed, executed, skipped, rates, costs}]}``."""
    payload = {
        "variants": [
            {
                "variant": summary.variant,
                "passed": summary.passed,
                "executed": summary.executed,
                "skipped": summary.skipped,
                "pass_rate": summary.pass_rate,
                "total_cost_usd": summary.total_cost_usd,
                "mean_cost_usd": summary.mean_cost_usd,
                "cost_per_pass_usd": summary.cost_per_pass_usd,
            }
            for summary in summaries
        ]
    }
    return json.dumps(payload, indent=2)
