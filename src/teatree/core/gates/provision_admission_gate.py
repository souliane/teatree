"""Resource-aware admission for parallel worktree provisioning.

``workspace provision`` runs several worktrees' provision subprocesses under
a bounded pool (souliane/teatree#2949). Two knobs govern admission:

1. **Concurrency cap** — :func:`resolve_provision_max_concurrency` derives
    a default from the host's core count
    (:func:`teatree.utils.ram_probe.default_provision_concurrency`) unless
    the operator pins an explicit ``provision_max_concurrency``.
2. **RAM floor** — :func:`check_provision_admission` uses the governor's
    available-memory reading and holds a new provision at its 4 GB brake floor.
    A held request is re-checked on the caller's retry cadence.
"""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.config import get_effective_settings
from teatree.core.admission.memory_hold import memory_hold_reason
from teatree.core.admission_governor import read_machine_signal
from teatree.core.admission_pressure import MachineSignal
from teatree.utils.disk_probe import disk_crit_percent, read_disk_used_percent
from teatree.utils.ram_probe import default_provision_concurrency


def resolve_provision_max_concurrency() -> int:
    """Effective concurrency cap for parallel worktree provisioning.

    ``provision_max_concurrency = 0`` (the default) auto-derives from the
    host's core count at each read, so the cap tracks the actual host instead
    of a number baked in at setup time. A positive value pins an explicit cap.
    """
    pinned = int(get_effective_settings().provision_max_concurrency)
    return pinned if pinned > 0 else default_provision_concurrency()


@dataclass(frozen=True, slots=True)
class ProvisionAdmissionVerdict:
    """Outcome of one admission check: proceed now, or hold and retry later."""

    ok: bool
    reason: str = ""

    @classmethod
    def hold(cls, reason: str) -> "ProvisionAdmissionVerdict":
        return cls(ok=False, reason=reason)

    @classmethod
    def allow(cls) -> "ProvisionAdmissionVerdict":
        return cls(ok=True, reason="")


def check_provision_admission(
    *,
    machine: MachineSignal | None = None,
    disk_used_percent: float | None = None,
    disk_probe: Callable[[], float | None] | None = None,
) -> ProvisionAdmissionVerdict:
    """Hold on critical disk or RAM pressure; an unreadable disk is unknown, not full.

    The governor's machine reading is the single source of memory pressure.
    """
    disk_sample = disk_used_percent if disk_used_percent is not None else (disk_probe or read_disk_used_percent)()
    disk_ceiling = disk_crit_percent()
    if disk_sample is not None and disk_sample >= disk_ceiling:
        return ProvisionAdmissionVerdict.hold(f"disk_pressure (used={disk_sample:.0f}% >= ceiling={disk_ceiling}%)")
    if reason := memory_hold_reason(machine if machine is not None else read_machine_signal()):
        return ProvisionAdmissionVerdict.hold(reason)
    return ProvisionAdmissionVerdict.allow()


__all__ = [
    "ProvisionAdmissionVerdict",
    "check_provision_admission",
    "resolve_provision_max_concurrency",
]
