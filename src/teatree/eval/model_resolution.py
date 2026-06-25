"""Resolve an :class:`~teatree.eval.models.EvalSpec` to its concrete model id.

The eval harness references models by ABSTRACT TIER, never a concrete id. A
scenario declares ``tier`` or ``phase`` (or nothing); this resolves either to a
concrete model id through the single
:data:`teatree.agents.model_tiering.TIER_MODELS` constant. An explicit ``model:``
pin (the escape hatch for a deliberate concrete-id pin, possibly ``model@effort``)
wins over both, and the matrix/benchmark lanes set ``model`` per cell — so this
resolver is a no-op when ``model`` is already set.

Precedence, highest first:

1.  ``spec.model`` (non-empty) — an explicit pin, returned unchanged (the
    ``@effort`` suffix flows through untouched for the variant parser).
2.  ``spec.tier`` — resolved through ``resolve_tier``.
3.  ``spec.phase`` — its tier via ``DEFAULT_PHASE_MODELS`` (default-tier fallback
    for an unmapped phase), then ``resolve_tier``.
4.  ``DEFAULT_TIER`` — the conservative default, resolved through ``resolve_tier``.
"""

from pathlib import Path

from teatree.agents.model_tiering import DEFAULT_PHASE_MODELS, DEFAULT_TIER, resolve_tier
from teatree.eval.models import EvalSpec


def resolve_eval_model(spec: EvalSpec, *, config_path: Path | None = None) -> str:
    """Return the concrete model id (or ``model@effort`` pin) for *spec*.

    See the module docstring for the precedence. The result is a concrete model
    id — never an abstract tier name — so the variant parser and every
    downstream consumer (model-presence check, ledger label, report) see a real
    model id.
    """
    if spec.model.strip():
        return spec.model
    if spec.tier.strip():
        return resolve_tier(spec.tier.strip(), config_path=config_path)
    if spec.phase.strip():
        tier = DEFAULT_PHASE_MODELS.get(spec.phase.strip(), DEFAULT_TIER)
        return resolve_tier(tier, config_path=config_path)
    return resolve_tier(DEFAULT_TIER, config_path=config_path)
