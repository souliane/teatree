"""Derive the CLI's short FAMILY aliases from the tier catalog.

``claude --model opus`` (and ``t3 eval run --models opus``) names a model
FAMILY, which the CLI resolves to whatever generation of that family is current.
:data:`~teatree.agents.model_tiering.TIER_MODELS` is teatree's answer to the same
question, so consumers derive the alias map from it rather than keeping a second
copy: a stale copy makes a lane REQUEST the previous generation while the
transcript REPORTS the current one, and every id-keyed comparison downstream —
fallback detection, the main-vs-auxiliary cost split — silently misfires.
"""

from teatree.agents import model_tiering
from teatree.core.cost import UNPRICED_TIER, tier_of_model


def family_alias_models() -> dict[str, str]:
    """Family short-name (``opus``/``sonnet``/``haiku``) → the catalog's concrete id.

    Read through the :mod:`~teatree.agents.model_tiering` module attribute, not a
    bound copy, so the catalog stays the single live source. Each tier's concrete
    id is folded onto its family via
    :func:`teatree.core.cost.tier_of_model` — the one family parser — so a tier
    bump carries every alias consumer with it.

    A tier whose id belongs to no known Claude family (an operator's swapped-in
    non-Claude pin) contributes no alias: teatree never invents a family name.
    Reads the SHIPPED catalog, not the ``agent_tier_models`` override — the CLI
    resolves its aliases from its own catalog, and an operator's dispatch
    override does not change what ``opus`` means to the CLI.
    """
    aliases: dict[str, str] = {}
    for model_id in model_tiering.TIER_MODELS.values():
        family = tier_of_model(model_id) if model_id else UNPRICED_TIER
        if family != UNPRICED_TIER:
            aliases[family] = model_id
    return aliases
