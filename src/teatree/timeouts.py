"""Multi-tier timeout configuration for lifecycle operations.

Resolution order (first non-None wins):
1. User settings — ``[teatree.timeouts]`` in ``~/.teatree.toml``
2. Overlay settings — ``OverlayBase.get_timeouts()``
3. Core defaults — ``TEATREE_TIMEOUTS`` in Django settings.py

Each tier returns a dict of ``{operation: seconds}``.  A value of ``0``
means "no timeout" for that operation.
"""

from dataclasses import dataclass, field

# Operation names used as keys.
SETUP = "setup"
START = "start"
DB_IMPORT = "db_import"
DOCKER_COMPOSE_UP = "docker_compose_up"
DOCKER_COMPOSE_DOWN = "docker_compose_down"
PROVISION_STEP = "provision_step"
PRE_RUN_STEP = "pre_run_step"

# Sane defaults (seconds).  Override at any tier.
CORE_DEFAULTS: dict[str, int] = {
    SETUP: 120,
    START: 60,
    DB_IMPORT: 180,
    DOCKER_COMPOSE_UP: 60,
    DOCKER_COMPOSE_DOWN: 30,
    PROVISION_STEP: 120,
    PRE_RUN_STEP: 60,
}


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    """Resolved timeout values for all operations."""

    values: dict[str, int] = field(default_factory=lambda: dict(CORE_DEFAULTS))

    def get(self, operation: str) -> int | None:
        """Return timeout in seconds, or ``None`` if timeouts are disabled."""
        val = self.values.get(operation, CORE_DEFAULTS.get(operation, 120))
        return val if val > 0 else None


def load_timeouts(overlay: object | None = None) -> TimeoutConfig:
    """Build a TimeoutConfig by merging all three tiers."""
    merged = dict(CORE_DEFAULTS)

    # Tier 3: Django settings (core defaults, already in merged)
    from django.conf import settings  # noqa: PLC0415

    django_timeouts = getattr(settings, "TEATREE_TIMEOUTS", None)
    if isinstance(django_timeouts, dict):
        merged.update(django_timeouts)

    # Tier 2: Overlay
    if overlay is not None and hasattr(overlay, "get_timeouts"):
        overlay_timeouts = overlay.get_timeouts()
        if overlay_timeouts:
            merged.update(overlay_timeouts)

    # Tier 1: User settings (~/.teatree.toml [teatree.timeouts])
    from teatree.config import load_config  # noqa: PLC0415

    user_timeouts = load_config().raw.get("teatree", {}).get("timeouts", {})
    if isinstance(user_timeouts, dict):
        merged.update({k: int(v) for k, v in user_timeouts.items()})

    return TimeoutConfig(values=merged)
