"""Live-registry resolution of a self-correctable config breach (#3665).

The orchestration half of :mod:`teatree.core.config_self_repair`: it reads every
REGISTERED harness's declared valid-provider set (built-ins plus any overlay
entry point) and feeds that table to the pure criterion. The agents-layer
registry and the core-layer config store are domain siblings that cannot import
each other, so only a ``teatree.loop`` module may compose them — the same reason
:mod:`teatree.loop.transient_requeue` lives here.
"""

from teatree.core.config_self_repair import ConfigRepair, plan_config_repair
from teatree.core.models import ConfigSetting


def repair_for_error(error: str) -> ConfigRepair | None:
    """The live-registry resolution for *error*, or ``None`` when it must page.

    Returns ``None`` when the store already holds the corrected value: the
    breach is no longer this setting's fault, so re-writing it would loop.
    """
    repair = plan_config_repair(error, valid_providers_by_harness=_registry_valid_providers())
    if repair is None:
        return None
    if ConfigSetting.objects.get_effective(repair.setting) == repair.value:
        return None
    return repair


def _registry_valid_providers() -> dict[str, frozenset[str]]:
    """Every registered harness's declared valid-provider set (built-ins + overlays)."""
    from teatree.agents.harness_registry import (  # noqa: PLC0415 — deferred: agents/loop import at call time
        registered_harness_names,
        valid_providers_for,
    )

    return {name: valid_providers_for(name) for name in registered_harness_names()}


__all__ = ["repair_for_error"]
