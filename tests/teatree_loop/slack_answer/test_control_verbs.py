"""Only an unambiguous whole-message verb controls a live task."""

import pytest

from teatree.loop.slack_answer.control_verbs import control_verb


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("pause", "pause"),
        ("Resume it, please!", "resume"),
        ("pause task #12.", "pause"),
        ("#12 pause", "pause"),
        ("#12 resume", "resume"),
        ("pause the migration until I review", None),
        ("please pause", None),
        ("pause #12 #13", None),
        ("resume because tests passed", None),
    ],
)
def test_only_a_bare_pause_or_resume_is_a_control_verb(message: str, expected: str | None) -> None:
    assert control_verb(message) == expected
