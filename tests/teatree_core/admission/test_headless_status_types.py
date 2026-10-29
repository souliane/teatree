"""The headless status payload exposes each reserved maintenance lane."""

from teatree.core.admission.headless_status_types import HeadlessAdmissionJson


def test_reserved_lane_fields_are_required_status_fields() -> None:
    assert {
        "architectural_review_lane",
        "architectural_review_occupied",
        "architectural_review_denied",
        "maintenance_lane",
        "maintenance_occupied",
        "maintenance_denied",
    } <= HeadlessAdmissionJson.__required_keys__
