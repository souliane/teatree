import logging
from unittest.mock import Mock

from teatree.core.models.extra_key_warning import warn_stripped_extra_keys


def test_undeclared_extra_key_is_warned_once_per_model() -> None:
    logger = Mock(spec=logging.Logger)
    seen: set[tuple[str, str]] = set()

    warn_stripped_extra_keys("Ticket", ["missing"], seen, logger)
    warn_stripped_extra_keys("Ticket", ["missing"], seen, logger)
    warn_stripped_extra_keys("Worktree", ["missing"], seen, logger)

    assert logger.warning.call_count == 2
    assert seen == {("Ticket", "missing"), ("Worktree", "missing")}
