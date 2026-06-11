"""The bounded "warm-equivalent" cost fit for the benchmark cost lane (#2192).

Billed cost stays the benchmark headline. This module computes the carefully-
bounded *diagnostic* alongside it: "what would each variant cost if every cell
fully benefited from the cache" — the cacheable input all priced at the 0.10x
read rate, removing the penalty cold cells paid.

It is price-table-free. For one variant it recovers ``(base_in_rate, out_rate)``
by least-squares over that variant's OWN clean cells, from the API's billed
identity (Anthropic's cache multipliers fixed at write 1.25x / read 0.10x):

    billed_i = base_in * effective_billed_input_i + out_rate * output_i

where ``effective_billed_input = input + 1.25*cache_creation + 0.10*cache_read``.
The warm-equivalent then re-prices every cell with the cacheable mass at the
read rate:

    warm = Σ_cells( base_in * (input + 0.10*cache_creation + 0.10*cache_read)
                    + out_rate * output )

**Never fabricate.** The fit is a 2x2 normal-equation solve. On a small slice
(fewer than :data:`_MIN_CLEAN_CELLS` clean cells) or an ill-conditioned normal
matrix (condition number above :data:`_MAX_CONDITION_NUMBER` — collinear
regressors when output variance is tiny or input is proportional to output),
both entry points return ``None`` and the renderer shows ``-``. A full
160-scenario suite is well-conditioned; the 8-cell smoke slice is usually
``-``. No numpy — a hand-rolled solve keeps the dependency surface flat.
"""

import dataclasses
import math

from teatree.eval.models import TokenUsage

#: Fewer clean cells than this and the 2-parameter fit is under-determined /
#: over-fit — degrade to ``None`` rather than report a fabricated rate.
_MIN_CLEAN_CELLS = 4

#: The 2x2 normal matrix's condition number (largest/smallest eigenvalue) above
#: this means the two regressors (billed-input vs output) are near-collinear —
#: the fit is numerically meaningless. This happens on small slices and when
#: output variance is tiny. Degrade to ``None``.
_MAX_CONDITION_NUMBER = 1e8

#: Anthropic's fixed cache-read multiplier — the rate the warm-equivalent prices
#: ALL cacheable input at (both reads and the would-be-cold writes).
_CACHE_READ_MULTIPLIER = 0.10


@dataclasses.dataclass(frozen=True)
class CostCell:
    """One clean cell feeding the fit: its token usage and its billed cost."""

    usage: TokenUsage
    billed_usd: float


@dataclasses.dataclass(frozen=True)
class FitResult:
    """The recovered per-variant rates from the billed-cost identity."""

    base_in_rate: float
    out_rate: float


def fit_rates(cells: list[CostCell]) -> FitResult | None:
    """Recover ``(base_in_rate, out_rate)`` by least-squares, or ``None`` when unsafe.

    Returns ``None`` when there are fewer than :data:`_MIN_CLEAN_CELLS` cells or
    the 2x2 normal matrix is ill-conditioned (see :data:`_MAX_CONDITION_NUMBER`)
    — never a fabricated rate.
    """
    if len(cells) < _MIN_CLEAN_CELLS:
        return None
    x1 = [cell.usage.effective_billed_input for cell in cells]
    x2 = [float(cell.usage.output) for cell in cells]
    billed = [cell.billed_usd for cell in cells]
    # Normal equations for b = [base_in, out] with no intercept:
    #   [s11 s12; s12 s22] @ b = [t1; t2]
    s11 = sum(a * a for a in x1)
    s12 = sum(a * b for a, b in zip(x1, x2, strict=True))
    s22 = sum(b * b for b in x2)
    t1 = sum(a * c for a, c in zip(x1, billed, strict=True))
    t2 = sum(b * c for b, c in zip(x2, billed, strict=True))
    det = s11 * s22 - s12 * s12
    # The conditioning guard already returns None for a singular/near-singular
    # matrix (det -> 0 drives the smaller eigenvalue to <= 0 -> inf), so a
    # non-degenerate det is guaranteed here; this is the single conditioning gate.
    if _condition_number(s11, s12, s22) > _MAX_CONDITION_NUMBER:
        return None
    base_in = (t1 * s22 - t2 * s12) / det
    out_rate = (s11 * t2 - s12 * t1) / det
    return FitResult(base_in_rate=base_in, out_rate=out_rate)


def warm_equivalent_cost(cells: list[CostCell]) -> float | None:
    """Total warm-equivalent cost over *cells*, or ``None`` when the fit is unsafe.

    Fits the variant's rates over its clean cells, then re-prices each cell with
    all cacheable input at the read rate. Returns ``None`` exactly when
    :func:`fit_rates` does (too few cells / ill-conditioned).
    """
    fit = fit_rates(cells)
    if fit is None:
        return None
    return sum(fit.base_in_rate * _warm_input(cell.usage) + fit.out_rate * cell.usage.output for cell in cells)


def _warm_input(usage: TokenUsage) -> float:
    """Cacheable input priced as if fully warm: ``input + 0.10*(cache_creation + cache_read)``."""
    return usage.input + _CACHE_READ_MULTIPLIER * (usage.cache_creation + usage.cache_read)


def _condition_number(s11: float, s12: float, s22: float) -> float:
    """Condition number (max/min eigenvalue) of the symmetric 2x2 ``[s11 s12; s12 s22]``.

    ``inf`` when the smaller eigenvalue is non-positive (a singular/degenerate
    matrix) so the caller degrades to ``None``.
    """
    trace = s11 + s22
    diff = math.sqrt((s11 - s22) ** 2 + 4.0 * s12 * s12)
    eig_max = (trace + diff) / 2.0
    eig_min = (trace - diff) / 2.0
    if eig_min <= 0.0:
        return math.inf
    return eig_max / eig_min
