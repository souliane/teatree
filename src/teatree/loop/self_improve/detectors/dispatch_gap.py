"""``DispatchGapDetector`` — "tasks waiting, no agents working" smell.

Smells the autonomous factory's flywheel stalling: live ``Task`` rows
are pending **and** the consolidation registry has no holder for the
current actor, so nothing is picking those tasks up.

Phase 1 keeps the detector strictly read-only — emits a warn-level
firing whose ladder ceiling is ``ticket`` per the issue plan, but the
``auto_fix`` flag is ``False`` (this is a smell to surface, not to
silently self-heal — picking the right agent is a judgment call).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from django.apps import apps

from teatree.core.models.task import Task
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport


def _consolidation_registry_holders() -> list[str]:
    """Read the agent-keyed consolidation registry.

    Returns the list of holding ``agent_id`` keys (an empty list when
    the file is missing or unreadable — the safe fail-open).  The
    registry layout mirrors ``hook_router._actor_key``.
    """
    import json  # noqa: PLC0415 — deferred: loaded only on this code path
    import os  # noqa: PLC0415 — deferred: loaded only on this code path

    base_env = os.environ.get("T3_LOOP_REGISTRY_DIR")
    base = Path(base_env) if base_env else Path.home() / ".local" / "share" / "teatree"
    registry_path = base / "consolidation-registry.json"
    if not registry_path.is_file():
        return []
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [str(k) for k in data]


@dataclass(slots=True)
class DispatchGapDetector:
    """No-one is picking up pending Task rows."""

    name: ClassVar[str] = "dispatch_gap"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "warn"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False

    def detect(self) -> list[DetectorReport]:
        task_model = apps.get_model("core", "Task")
        pending_count = task_model.objects.filter(status=Task.Status.PENDING).count()
        if pending_count == 0:
            return []
        holders = _consolidation_registry_holders()
        if holders:
            return []
        identity = f"pending={pending_count}"
        return [
            DetectorReport(
                detector=self.name,
                dedup_key=canonical_key(self.name, "global"),
                state_hash=state_hash("pending", pending_count),
                severity=self.severity,
                max_rung=self.max_rung,
                summary=f"{pending_count} pending task(s) and no consolidation holder",
                payload={"pending_count": pending_count, "identity": identity},
                auto_fix=self.auto_fix,
            )
        ]

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]
