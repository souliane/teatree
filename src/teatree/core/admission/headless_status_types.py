"""Structured status payload for the headless admission governor."""

from typing import TypedDict


class HeadlessAdmissionJson(TypedDict):
    ceiling: int
    cores: int
    per_core: float
    machine_ceiling: int
    weekly_pace: float | None
    expensive_lane: int
    cheap_lane: int
    review_lane: int
    retiring_lane: int
    expensive_occupied: int
    cheap_occupied: int
    review_occupied: int
    retiring_occupied: int
    review_phases: list[str]
    retiring_phases: list[str]
    pressure: float
    band: str
    expensive_denied: str | None
    cheap_denied: str | None
    review_denied: str | None
    retiring_denied: str | None
    architectural_review_lane: int
    architectural_review_occupied: int
    architectural_review_denied: str | None
    maintenance_lane: int
    maintenance_occupied: int
    maintenance_denied: str | None
