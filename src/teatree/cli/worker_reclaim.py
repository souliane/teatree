"""The worker-venue reclaim invoked by deploy before it builds an image."""

import time

from teatree.utils.django_bootstrap import ensure_django


def run_reclaim(*, budget_seconds: int) -> tuple[str, ...]:
    ensure_django()

    from teatree.config import get_effective_settings  # noqa: PLC0415 — settings read needs Django
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — deferred ORM
    from teatree.loop.mechanical_artifacts import sweep_artifacts  # noqa: PLC0415 — deferred ORM
    from teatree.loop.mechanical_resources import free_resources  # noqa: PLC0415 — deferred ORM
    from teatree.loop.scanners.resource_pressure import measure_resources  # noqa: PLC0415 — deferred ORM

    settings = get_effective_settings()
    started = time.monotonic()
    reading = measure_resources()
    free_gb = reading.disk_free_gb if not reading.disk_probe_degraded else None
    pressure = {
        "free_gb": free_gb,
        "disk_warn_free_gb": settings.disk_warn_free_gb,
        "disk_crit_free_gb": settings.disk_crit_free_gb,
    }
    free_resources(
        {
            **pressure,
            "resource": "disk",
            "disk_cache_allowlist": settings.disk_cache_allowlist,
            "budget_seconds": min(budget_seconds / 2, 45),
        }
    )
    remaining = budget_seconds - (time.monotonic() - started)
    if remaining > 0:
        sweep_artifacts({**pressure, "artifact_idle_days": settings.artifact_idle_days, "budget_seconds": remaining})
    marker = ResourcePressureMarker.load()
    return tuple(line for line in (marker.last_plan, marker.last_artifact_plan) if line)
