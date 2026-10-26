from teatree.core.cycle_time.eta import format_minutes, queue_waves, remaining_given_elapsed


def test_running_step_uses_only_samples_that_outlast_elapsed() -> None:
    assert remaining_given_elapsed([30, 40, 50, 60, 70], 45) == (15, 21)
    assert remaining_given_elapsed([30, 40, 50, 60, 70], 71) is None


def test_queue_wait_counts_the_item_and_occupied_seats() -> None:
    assert queue_waves(ahead=4, occupied=0, seats=2) == 2
    assert queue_waves(ahead=0, occupied=0, seats=2) == 0
    assert queue_waves(ahead=0, occupied=2, seats=2) == 1
    assert queue_waves(ahead=0, occupied=0, seats=0) is None


def test_minutes_format_is_range_ready() -> None:
    assert format_minutes(63) == "1h03m"
    assert format_minutes(118.2) == "1h58m"
