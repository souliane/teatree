"""Periodic provision-smoke scanner (#1308).

Companion to the ``t3 dogfood overlay-provision-smoke`` management
command: the loop queues a ``dogfood_smoke`` task once per fire of the
daily ``dogfood`` ``Loop`` row so latent CLI bugs in the overlay
provision path surface in the loop, not in the user's next E2E
session. One of the periodic task-queuing family that share
:class:`teatree.loop.scanners.phase_cadence.PhaseCadence` — a fixed-rate
platform behaviour, not coupled to delivery velocity.

The scanner only *schedules*; the dispatcher picks up the queued task
and shells out to ``t3 dogfood overlay-provision-smoke``. Failures DM
the user via :mod:`teatree.core.notify` from inside the management command,
so the scanner has no responsibility for the verdict pipeline beyond
queueing the task its row asked for.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.modelkit.phases import DOGFOOD_SMOKE_PHASE
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.phase_cadence import PhaseCadence

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.config.settings import UserSettings


@dataclass(slots=True)
class ProvisionSmokeScanner:
    """Queue a periodic ``dogfood_smoke`` task per overlay anchor.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings`. The on/off decision is the ``dogfood``
    ``Loop`` row and the active preset; the scanner itself always scans when invoked.
    """

    overlay_name: str
    skill: str = "dogfood-smoke"
    name: str = "provision_smoke"

    def scan(self) -> list[ScanSignal]:
        if not self.overlay_name:
            return []
        cadence = PhaseCadence(self.overlay_name, phase=DOGFOOD_SMOKE_PHASE)
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
    resolve_settings: "Callable[[], UserSettings]",
    discover_active_overlay: "Callable[[], object]",
    canonical_fallback: str,
) -> "ProvisionSmokeScanner | None":
    """Resolve ``UserSettings`` + active overlay into a wired scanner (#1308).

    The overlay anchor is resolved via the injected ``discover_active_overlay``
    callable, with ``dogfood_smoke_overlay`` as the explicit pin and
    ``canonical_fallback`` (e.g. ``t3-teatree``) as the defensive default. The callables
    are injected so global_scanner_factories keeps wiring lean and tests can stub each
    layer independently.

    ``resolve_settings`` returns the EFFECTIVE settings, never a raw config: the seam
    used to take ``load_config`` and read ``.user`` off it, which is the dataclass
    defaults, so a stored ``dogfood_smoke_overlay`` never reached the scanner.
    """
    settings = resolve_settings()
    overlay_name = settings.dogfood_smoke_overlay
    if not overlay_name:
        active = discover_active_overlay()
        overlay_name = getattr(active, "name", "") or canonical_fallback
    return ProvisionSmokeScanner(
        overlay_name=overlay_name,
        skill=settings.dogfood_smoke_skill,
    )


__all__ = [
    "DOGFOOD_SMOKE_PHASE",
    "ProvisionSmokeScanner",
    "build_provision_smoke_scanner",
]
