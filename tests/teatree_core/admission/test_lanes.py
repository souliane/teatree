"""The admission lane value types retain their seat arithmetic."""

from teatree.core.admission.lanes import LaneBound, LaneOccupancy, LaneWidths
from teatree.core.admission.write_ceiling import AdmissionCeiling


def test_retiring_class_uses_the_setting_width() -> None:
    widths = LaneWidths(ceiling=AdmissionCeiling(cores=8, per_core=0.5, pace=None), review=3)

    assert widths.retiring == 3


def test_cheap_total_is_one_wider_than_each_class() -> None:
    widths = LaneWidths(ceiling=AdmissionCeiling(cores=8, per_core=0.5, pace=None), review=3)

    assert widths.cheap == 4
    assert widths.expensive == 4


def test_lane_bound_is_spent_at_its_last_seat() -> None:
    bound = LaneBound(ceiling=2, headroom=1, admitted=1)

    assert bound.spent()


def test_cheap_occupancy_sums_both_classes() -> None:
    occupied = LaneOccupancy(expensive=3, review=2, retiring=1)

    assert occupied.cheap == 3
