from datetime import UTC, datetime, timedelta

from teatree.core.cycle_time.work_intervals import subtract_intervals


def test_subtract_intervals_clips_overlapping_removals() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def at(minutes: int) -> datetime:
        return start + timedelta(minutes=minutes)

    assert subtract_intervals(((at(0), at(90)),), ((at(20), at(50)), (at(40), at(70)))) == (
        (at(0), at(20)),
        (at(70), at(90)),
    )
    assert subtract_intervals(((at(0), at(20)),), ((at(30), at(40)),)) == ((at(0), at(20)),)
    assert subtract_intervals(((at(0), at(20)),), ((at(0), at(30)),)) == ()
