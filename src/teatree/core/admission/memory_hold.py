"""One available-memory guard for provision and self-improve work."""

from teatree.core.admission_pressure import RAM_BRAKE_FLOOR_GB, MachineSignal


def memory_hold_reason(machine: MachineSignal) -> str | None:
    source = machine.memory_source or machine.source or "reading unknown"
    available = machine.ram_available_gb
    if available is None:
        return None
    if available <= RAM_BRAKE_FLOOR_GB:
        return f"memory_pressure ({available:.1f} GB available at/under {RAM_BRAKE_FLOOR_GB:.0f} GB floor; {source})"
    return None
