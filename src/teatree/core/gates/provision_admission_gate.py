"""Resource-aware admission for parallel worktree provisioning.

``workspace provision`` runs several worktrees' provision subprocesses under
a bounded pool (souliane/teatree#2949). Two knobs govern admission:

1. **Concurrency cap** — :func:`resolve_provision_max_concurrency` derives
    a default from the host's core count
    (:func:`teatree.utils.ram_probe.default_provision_concurrency`) unless
    the operator pins an explicit ``provision_max_concurrency``.
2. **RAM ceiling** — :func:`check_provision_admission` HOLDS a new
    provision (never starts it) once host RAM crosses
    ``provision_ram_ceiling_percent``, mirroring the self-improve budget
    gate's RAM guardrail so a cold multi-repo provision never pushes the
    host into OOM. A held request is not lost: the caller re-checks on its
    own retry cadence and the request drains automatically once RAM frees.
"""

from dataclasses import dataclass

from teatree.config import get_effective_settings
from teatree.utils.ram_probe import default_provision_concurrency, read_ram_used_percent


def resolve_provision_max_concurrency() -> int:
    """Effective concurrency cap for parallel worktree provisioning.

    ``provision_max_concurrency = 0`` (the default) auto-derives from the
    host's core count at each read, so the cap tracks the actual host instead
    of a number baked in at setup time. A positive value pins an explicit cap.
    """
    pinned = int(get_effective_settings().provision_max_concurrency)
    return pinned if pinned > 0 else default_provision_concurrency()


def resolve_provision_ram_ceiling_percent() -> int:
    """The configured RAM-used-percent ceiling above which admission holds."""
    return int(get_effective_settings().provision_ram_ceiling_percent)


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


def check_provision_admission(*, ram_used_percent: float | None = None) -> ProvisionAdmissionVerdict:
    """Return ``hold(reason)`` when RAM is at/above the ceiling, else ``allow()``.

    Tests inject *ram_used_percent* directly rather than mocking the platform
    probe (mirrors :func:`teatree.loop.self_improve.budget.precheck_budget`).
    """
    sample = ram_used_percent if ram_used_percent is not None else read_ram_used_percent()
    ceiling = resolve_provision_ram_ceiling_percent()
    if sample >= ceiling:
        return ProvisionAdmissionVerdict.hold(f"ram_pressure (used={sample:.0f}% >= ceiling={ceiling}%)")
    return ProvisionAdmissionVerdict.allow()


__all__ = [
    "ProvisionAdmissionVerdict",
    "check_provision_admission",
    "resolve_provision_max_concurrency",
    "resolve_provision_ram_ceiling_percent",
]
