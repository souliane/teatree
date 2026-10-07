"""The headless WRITE ceiling's arithmetic, independent of where its factor is read from."""

import pytest

from teatree.core.admission.write_ceiling import AdmissionCeiling


@pytest.mark.parametrize(
    ("cores", "per_core", "pace", "seats"),
    [(25, 1.16, None, 29), (100, 1.0, 0.29, 29)],
)
def test_float_error_never_costs_a_seat(cores: int, per_core: float, pace: float | None, seats: int) -> None:
    assert AdmissionCeiling(cores=cores, per_core=per_core, pace=pace).value == seats
