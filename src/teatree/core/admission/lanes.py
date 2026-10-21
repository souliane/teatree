"""Seat counts and widths shared by admission decisions and status."""

from dataclasses import dataclass

from teatree.core.admission.write_ceiling import AdmissionCeiling


@dataclass
class LaneBound:
    """A lane's ceiling and seats booked during one admission pass.

    Headroom is local to the pass; the conditional seat write rechecks the ceiling
    because another process cannot see this pass's counter.
    """

    ceiling: int | None = None
    headroom: int | None = None
    admitted: int = 0

    def spent(self) -> bool:
        return self.headroom is not None and self.admitted >= self.headroom


@dataclass(frozen=True)
class LaneOccupancy:
    expensive: int
    review: int
    retiring: int

    @property
    def cheap(self) -> int:
        return self.review + self.retiring


@dataclass(frozen=True)
class LaneWidths:
    """Each cheap class has width W inside a shared lane of W+1 seats."""

    ceiling: AdmissionCeiling
    review: int

    @property
    def expensive(self) -> int:
        return self.ceiling.value

    @property
    def retiring(self) -> int:
        return self.review

    @property
    def cheap(self) -> int:
        return self.review + 1
