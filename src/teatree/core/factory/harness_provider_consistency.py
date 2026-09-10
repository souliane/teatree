"""Which scopes have an inconsistent (agent_harness, agent_harness_provider) pair (souliane/teatree#4726 LOC split).

A pair the harness registry would refuse at dispatch — set before the
write-time guard existed, or via a path the guard does not cover — otherwise
fails EVERY dispatch in that scope, one repair-halt at a time. Surfacing it as
a single loud health-red replaces that per-task flood with one visible signal.
The effective pair is resolved exactly as dispatch resolves it
(:func:`~teatree.config.get_effective_settings`, env → DB → default) for the
global/active scope and each registered overlay; an overlay-registered harness
is unconstrained here (its constraint lives in the open registry). Per-scope
fail-open so one broken resolve never suppresses another scope's signal.

Separated from :mod:`teatree.core.factory.operational_health` the way
``dream_fallen_behind`` is — the aggregator owns folding a per-scope mismatch
into a :class:`~teatree.core.factory.operational_health.HealthSignal`, this
module owns resolving and comparing the pair itself.
"""

import logging
from dataclasses import dataclass

from teatree.core.overlay_loader import get_all_overlays
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScopeMismatch:
    """One scope whose effective harness/provider pair the registry would refuse."""

    scope: str | None
    reason: str


def harness_provider_mismatches() -> tuple[list[ScopeMismatch], list[str]]:
    """Per-scope mismatches, and the scope labels a broken resolve could not read."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred to keep the module cold-import cheap
    from teatree.config.cross_key_consistency import (  # noqa: PLC0415 — deferred: same cold-import discipline
        check_harness_provider_pair,
    )

    mismatches: list[ScopeMismatch] = []
    unread: list[str] = []
    scopes: list[str | None] = [None, *sorted(get_all_overlays())]
    for scope in scopes:
        label = scope or "global"
        try:
            settings = get_effective_settings(scope)
            provider = settings.agent_harness_provider
            reason = check_harness_provider_pair(
                settings.agent_harness,
                provider.value if provider is not None else None,
            )
        except Exception:  # noqa: BLE001 — fail-open: a broken health read must never crash the tick or blank the chip
            unread.append(label)
            warn_throttled(
                logger,
                f"health-harness-pair:{label}",
                "harness/provider consistency health read failed for scope %s — skipped",
                label,
                exc_info=True,
            )
            continue
        if reason is not None:
            mismatches.append(ScopeMismatch(scope, reason))
    return mismatches, unread
