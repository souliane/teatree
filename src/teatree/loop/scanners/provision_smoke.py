"""Periodic provision-smoke scanner (#1308).

Companion to the ``t3 dogfood overlay-provision-smoke`` management
command: the loop queues a ``dogfood_smoke`` task once per cadence
window (default 24h — nightly) so latent CLI bugs in the overlay
provision path surface in the loop, not in the user's next E2E
session. One of the periodic task-queuing family that share
:class:`teatree.loop.scanners.phase_cadence.PhaseCadence` — a fixed-rate
platform behaviour, not coupled to delivery velocity.

The scanner only *schedules*; the dispatcher picks up the queued task
and shells out to ``t3 dogfood overlay-provision-smoke``. Failures DM
the user via :mod:`teatree.core.notify` from inside the management command,
so the scanner has no responsibility for the verdict pipeline beyond
keeping its cadence honest.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.modelkit.phases import DOGFOOD_SMOKE_PHASE
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.phase_cadence import PhaseCadence

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(slots=True)
class ProvisionSmokeScanner:
    """Queue a periodic ``dogfood_smoke`` task per overlay anchor.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings`. The on/off decision lives at
    the wiring layer (``dogfood_smoke_disabled`` in core config); the
    scanner itself always scans when invoked.
    """

    overlay_name: str
    skill: str = "dogfood-smoke"
    cadence_hours: int = 24
    name: str = "provision_smoke"

    def scan(self) -> list[ScanSignal]:
        if not self.overlay_name:
            return []
        cadence = PhaseCadence(self.overlay_name, phase=DOGFOOD_SMOKE_PHASE, cadence_hours=self.cadence_hours)
        if cadence.in_flight_exists():
            return []

        trigger = cadence.evaluate_trigger(now=timezone.now(), last_run_at=cadence.last_run_at())
        if trigger is None:
            return []

        task = cadence.queue_task(
            placeholder_issue_url=f"dogfood-smoke://{self.overlay_name}",
            agent_id=f"dogfood-smoke-{self.overlay_name}",
            execution_reason=f"Periodic provision smoke ({trigger}) via skill: {self.skill}",
            subject=f"Provision smoke: {self.overlay_name}",
            log_label="ProvisionSmokeScanner",
        )
        if task is None:
            return []
        return [
            ScanSignal(
                kind="dogfood_smoke.queued",
                summary=f"dogfood smoke queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "skill": self.skill,
                    "phase": DOGFOOD_SMOKE_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                },
            ),
        ]


def build_provision_smoke_scanner(
    *,
    load_config: "Callable[[], object]",
    discover_active_overlay: "Callable[[], object]",
    canonical_fallback: str,
) -> "ProvisionSmokeScanner | None":
    """Resolve ``UserSettings`` + active overlay into a wired scanner (#1308).

    Returns ``None`` when ``dogfood_smoke_disabled = true`` (the escape
    hatch). The overlay anchor is resolved via the injected
    ``discover_active_overlay`` callable, with ``dogfood_smoke_overlay``
    as the explicit pin and ``canonical_fallback`` (e.g. ``t3-teatree``)
    as the defensive default. The callables are injected so global_scanner_factories
    keeps wiring lean and tests can stub each layer independently.
    """
    settings = load_config().user  # type: ignore[attr-defined]
    if settings.dogfood_smoke_disabled:
        return None
    overlay_name = settings.dogfood_smoke_overlay
    if not overlay_name:
        active = discover_active_overlay()
        overlay_name = getattr(active, "name", "") or canonical_fallback
    return ProvisionSmokeScanner(
        overlay_name=overlay_name,
        skill=settings.dogfood_smoke_skill,
        cadence_hours=settings.dogfood_smoke_cadence_hours,
    )


__all__ = [
    "DOGFOOD_SMOKE_PHASE",
    "ProvisionSmokeScanner",
    "build_provision_smoke_scanner",
]
