"""``warn_throttled`` surfaces a persistent fault once per window, then quiets to debug."""

import logging

import pytest

from teatree.utils.throttled_log import reset_throttle, warn_throttled

_logger = logging.getLogger("teatree.tests.throttled")


@pytest.fixture(autouse=True)
def _clean_throttle_state() -> None:
    reset_throttle()


def test_first_occurrence_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_logger.name):
        warn_throttled(_logger, "probe", "health read failed")
    records = [r for r in caplog.records if r.name == _logger.name]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "health read failed" in records[0].getMessage()


def test_recurrence_within_window_drops_to_debug(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_logger.name):
        warn_throttled(_logger, "probe", "health read failed", window_seconds=3600)
        warn_throttled(_logger, "probe", "health read failed", window_seconds=3600)
        warn_throttled(_logger, "probe", "health read failed", window_seconds=3600)
    levels = [r.levelno for r in caplog.records if r.name == _logger.name]
    # First surfaces at warning; the persistent recurrences stay at debug so a
    # per-tick fault does not spam the log every beat.
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]


def test_distinct_keys_each_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_logger.name):
        warn_throttled(_logger, "collector-a", "a failed", window_seconds=3600)
        warn_throttled(_logger, "collector-b", "b failed", window_seconds=3600)
    levels = [r.levelno for r in caplog.records if r.name == _logger.name]
    assert levels == [logging.WARNING, logging.WARNING]


def test_expired_window_warns_again(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_logger.name):
        # window_seconds=0 means the throttle window has always elapsed, so every
        # call is treated as a fresh occurrence and re-warns.
        warn_throttled(_logger, "probe", "health read failed", window_seconds=0)
        warn_throttled(_logger, "probe", "health read failed", window_seconds=0)
    levels = [r.levelno for r in caplog.records if r.name == _logger.name]
    assert levels == [logging.WARNING, logging.WARNING]
