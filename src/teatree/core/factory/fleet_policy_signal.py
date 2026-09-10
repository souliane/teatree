"""Whether this box's fleet loop declaration is unsatisfiable (souliane/teatree#4726 LOC split).

``deploy/entrypoint.sh`` resolves a contradictory ``TEATREE_DISABLED_LOOPS``
correctly (it prunes the unmaskable names and continues rather than crash-looping
init on the config the box already shipped) and warns on stderr — but that stderr
scrolls away with the deploy log, so a declaration that masks NOTHING, and that
silently displaced the built-in default, persists unnoticed across every redeploy.
Compose passes the env file to every service, so the same declaration the init
role read is readable here.

Separated from :mod:`teatree.core.factory.operational_health` the way
``dream_fallen_behind`` is — the aggregator owns folding a signal into a
:class:`~teatree.core.factory.operational_health.HealthSignal`, this module owns
what "unsatisfiable" means.
"""


def fleet_policy_violation() -> str | None:
    """The contradiction reason, or ``None`` when the declaration is sound.

    An env read cannot fail, so callers have no ``unread`` state to track for it.
    """
    import os  # noqa: PLC0415 — deferred: keeps the module cold-import cheap, like the sibling collectors

    from teatree.config.fleet_policy import (  # noqa: PLC0415 — deferred: same cold-import discipline
        FLEET_DISABLED_VARIABLE,
        FLEET_ENABLED_VARIABLE,
        fleet_policy_contradiction,
    )

    return fleet_policy_contradiction(
        enabled_raw=os.environ.get(FLEET_ENABLED_VARIABLE),
        disabled_raw=os.environ.get(FLEET_DISABLED_VARIABLE),
    )
