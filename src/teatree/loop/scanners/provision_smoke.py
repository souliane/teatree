"""Periodic provision-smoke scanner (#1308).

Companion to the ``t3 dogfood overlay-provision-smoke`` management
command: the loop queues a ``dogfood_smoke`` task once per cadence
window (default 24h — nightly) so latent CLI bugs in the overlay
provision path surface in the loop, not in the user's next E2E
session. Mirrors :class:`teatree.loop.scanners.scanning_news.ScanningNewsScanner`
in shape — a fixed-rate platform behaviour, not coupled to delivery
velocity.

The scanner only *schedules*; the dispatcher picks up the queued task
and shells out to ``t3 dogfood overlay-provision-smoke``. Failures DM
the user via :mod:`teatree.notify` from inside the management command,
so the scanner has no responsibility for the verdict pipeline beyond
keeping its cadence honest.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.models import Session as _Session
    from teatree.core.models import Task as _Task
    from teatree.core.models import Ticket as _Ticket

logger = logging.getLogger(__name__)


#: Canonical phase token written to ``Task.phase`` for smoke tasks.
DOGFOOD_SMOKE_PHASE = "dogfood_smoke"

#: States that mean a smoke task is still in-flight (cannot queue a dupe).
_IN_FLIGHT_TASK_STATES: frozenset[str] = frozenset({"pending", "claimed"})


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
        if self._in_flight_task_exists():
            return []

        now = timezone.now()
        last_run_at = self._last_run_at()
        trigger = self._evaluate_trigger(now=now, last_run_at=last_run_at)
        if trigger is None:
            return []

        task = self._queue_task(trigger=trigger)
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

    def _in_flight_task_exists(self) -> bool:
        task_model = _task_model()
        if task_model is None:
            return False
        return task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=DOGFOOD_SMOKE_PHASE,
            status__in=_IN_FLIGHT_TASK_STATES,
        ).exists()

    def _last_run_at(self) -> object:
        task_model = _task_model()
        if task_model is None:
            return None
        aggregate = task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=DOGFOOD_SMOKE_PHASE,
        ).aggregate(ts=Max("session__started_at"))
        return aggregate["ts"]

    def _evaluate_trigger(self, *, now: object, last_run_at: object) -> str | None:
        if last_run_at is None:
            return "bootstrap"
        elapsed_hours = (now - last_run_at).total_seconds() / 3600.0  # type: ignore[operator]
        if elapsed_hours >= self.cadence_hours:
            return "cadence"
        return None

    def _queue_task(self, *, trigger: str) -> "_Task | None":
        ticket_model = _ticket_model()
        task_model = _task_model()
        session_model = _session_model()
        if ticket_model is None or task_model is None or session_model is None:
            return None
        try:
            with transaction.atomic():
                ticket, _created = ticket_model.objects.get_or_create(
                    issue_url=self._placeholder_issue_url(),
                    defaults={"overlay": self.overlay_name, "role": "author"},
                )
                if ticket.overlay != self.overlay_name:
                    ticket.overlay = self.overlay_name
                    ticket.save(update_fields=["overlay"])
                session = session_model.objects.create(
                    overlay=self.overlay_name,
                    ticket=ticket,
                    agent_id=f"dogfood-smoke-{self.overlay_name}",
                )
                return task_model.objects.create(
                    ticket=ticket,
                    session=session,
                    phase=DOGFOOD_SMOKE_PHASE,
                    execution_target=task_model.ExecutionTarget.HEADLESS,
                    execution_reason=(f"Periodic provision smoke ({trigger}) via skill: {self.skill}"),
                )
        except Exception:
            logger.exception("ProvisionSmokeScanner: failed to queue smoke task")
            return None

    def _placeholder_issue_url(self) -> str:
        return f"dogfood-smoke://{self.overlay_name}"


def _ticket_model() -> "type[_Ticket] | None":
    try:
        return cast("type[_Ticket]", apps.get_model("core", "Ticket"))
    except Exception:  # noqa: BLE001
        return None


def _task_model() -> "type[_Task] | None":
    try:
        return cast("type[_Task]", apps.get_model("core", "Task"))
    except Exception:  # noqa: BLE001
        return None


def _session_model() -> "type[_Session] | None":
    try:
        return cast("type[_Session]", apps.get_model("core", "Session"))
    except Exception:  # noqa: BLE001
        return None


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
