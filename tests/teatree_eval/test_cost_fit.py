"""The bounded warm-equivalent cost fit (#2192).

A pure 2x2 normal-equation least-squares over a variant's OWN clean cells
recovers its ``(base_in_rate, out_rate)`` from the billed-cost identity, then
re-prices the cacheable input at the cache-read rate to answer "what would this
variant cost if every cell fully benefited from the cache". Degrades to ``None``
— never a fabricated number — on too-few cells or an ill-conditioned matrix.
"""

import pytest

from teatree.eval.cost_fit import CostCell, fit_rates, warm_equivalent_cost
from teatree.eval.models import TokenUsage

# Anthropic billed-input multipliers (fixed): write 1.25x, read 0.10x.
_W, _R = 1.25, 0.10


def _billed(usage: TokenUsage, *, base_in: float, out: float) -> float:
    return base_in * usage.effective_billed_input + out * usage.output


def _cell(  # noqa: PLR0913 — test-data builder: one kwarg per TokenUsage/rate field a case varies.
    inp: int, cc: int, cr: int, out: int, *, base_in: float, out_rate: float
) -> CostCell:
    usage = TokenUsage(input=inp, cache_creation=cc, cache_read=cr, output=out)
    return CostCell(usage=usage, billed_usd=_billed(usage, base_in=base_in, out=out_rate))


class TestFitRates:
    def test_recovers_known_rates_on_a_well_conditioned_set(self) -> None:
        base_in, out_rate = 3e-6, 1.5e-5
        cells = [
            _cell(1000, 200, 5000, 300, base_in=base_in, out_rate=out_rate),
            _cell(800, 600, 200, 1200, base_in=base_in, out_rate=out_rate),
            _cell(1500, 100, 9000, 50, base_in=base_in, out_rate=out_rate),
            _cell(400, 900, 100, 2000, base_in=base_in, out_rate=out_rate),
        ]
        fit = fit_rates(cells)
        assert fit is not None
        assert fit.base_in_rate == pytest.approx(base_in, rel=1e-6)
        assert fit.out_rate == pytest.approx(out_rate, rel=1e-6)

    def test_too_few_cells_yields_none(self) -> None:
        base_in, out_rate = 3e-6, 1.5e-5
        cells = [
            _cell(1000, 200, 5000, 300, base_in=base_in, out_rate=out_rate),
            _cell(800, 600, 200, 1200, base_in=base_in, out_rate=out_rate),
            _cell(1500, 100, 9000, 50, base_in=base_in, out_rate=out_rate),
        ]
        assert fit_rates(cells) is None

    def test_collinear_regressors_are_ill_conditioned_and_yield_none(self) -> None:
        # Output is proportional to input in every cell, so the two regressors
        # (billed-input, output) are collinear -> the 2x2 normal matrix is
        # singular and the fit is numerically meaningless.
        base_in, out_rate = 3e-6, 1.5e-5
        cells = [_cell(n, 0, 0, n // 10, base_in=base_in, out_rate=out_rate) for n in (1000, 2000, 3000, 4000, 5000)]
        assert fit_rates(cells) is None


class TestWarmEquivalentCost:
    def test_warm_equivalent_is_below_billed_when_cold_writes_exist(self) -> None:
        base_in, out_rate = 3e-6, 1.5e-5
        cells = [
            _cell(1000, 2000, 5000, 300, base_in=base_in, out_rate=out_rate),
            _cell(800, 600, 200, 1200, base_in=base_in, out_rate=out_rate),
            _cell(1500, 1000, 9000, 50, base_in=base_in, out_rate=out_rate),
            _cell(400, 900, 100, 2000, base_in=base_in, out_rate=out_rate),
        ]
        billed_total = sum(c.billed_usd for c in cells)
        warm = warm_equivalent_cost(cells)
        assert warm is not None
        assert warm < billed_total

    def test_warm_equivalent_reprices_cache_creation_at_read_rate(self) -> None:
        base_in, out_rate = 3e-6, 1.5e-5
        cells = [
            _cell(1000, 2000, 5000, 300, base_in=base_in, out_rate=out_rate),
            _cell(800, 600, 200, 1200, base_in=base_in, out_rate=out_rate),
            _cell(1500, 1000, 9000, 50, base_in=base_in, out_rate=out_rate),
            _cell(400, 900, 100, 2000, base_in=base_in, out_rate=out_rate),
        ]
        expected = sum(
            base_in * (c.usage.input + _R * c.usage.cache_creation + _R * c.usage.cache_read)
            + out_rate * c.usage.output
            for c in cells
        )
        warm = warm_equivalent_cost(cells)
        assert warm == pytest.approx(expected, rel=1e-6)

    def test_too_few_cells_yields_none(self) -> None:
        cells = [_cell(1000, 200, 5000, 300, base_in=3e-6, out_rate=1.5e-5)]
        assert warm_equivalent_cost(cells) is None

    def test_ill_conditioned_yields_none(self) -> None:
        cells = [_cell(n, 0, 0, n // 10, base_in=3e-6, out_rate=1.5e-5) for n in (1000, 2000, 3000, 4000)]
        assert warm_equivalent_cost(cells) is None
