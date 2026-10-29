from teatree.core.admission.memory_hold import memory_hold_reason
from teatree.core.admission_pressure import MachineSignal


def _machine(available_gb: float | None) -> MachineSignal:
    return MachineSignal(cores=8, load1=1.0, ram_available_gb=available_gb, memory_source="container reading")


def test_memory_hold_uses_the_governor_brake_floor() -> None:
    assert (
        memory_hold_reason(_machine(4.0)) == "memory_pressure (4.0 GB available at/under 4 GB floor; container reading)"
    )
    assert memory_hold_reason(_machine(4.1)) is None


def test_unreadable_memory_does_not_invent_pressure() -> None:
    assert memory_hold_reason(_machine(None)) is None
