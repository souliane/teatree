from teatree.core.models.thread_binding import slack_command_id


def test_slack_command_id_is_bounded_and_overlay_qualified() -> None:
    first = slack_command_id("a" * 64, "1" * 64)

    assert first == slack_command_id("a" * 64, "1" * 64)
    assert first != slack_command_id("b" * 64, "1" * 64)
    assert first != slack_command_id("a" * 64, "2" * 64)
    assert len(first.encode()) <= 128
