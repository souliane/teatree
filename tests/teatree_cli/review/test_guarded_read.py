"""A failed read in the review CLI is distinguishable from a clean one (#3509).

Four sites caught a bare ``Exception`` and returned a neutral value with no log, so
"the read failed" and "there is nothing there" were the same answer — and in three of
them the neutral value was also the PERMISSIVE one, so a failed read read as a passed
gate. :mod:`teatree.cli.review.guarded_read` is the one helper they now share.
"""

import logging

import pytest

from teatree.cli.review.guarded_read import ReadRefusedError, guarded_read, read_or_refuse

_READ_FAILURE = "network down"


class _StubReadError(RuntimeError):
    """A stand-in for whatever the forge client raises when a read fails."""

    def __init__(self) -> None:
        super().__init__(_READ_FAILURE)


class TestGuardedReadDistinguishesFailureFromEmpty:
    def test_a_clean_read_reports_not_failed(self) -> None:
        outcome = guarded_read("mr author", lambda: "someone", neutral="")
        assert outcome.value == "someone"
        assert outcome.failed is False

    def test_a_genuine_empty_reports_not_failed(self) -> None:
        outcome = guarded_read("mr author", lambda: "", neutral="")
        assert outcome.value == ""
        assert outcome.failed is False

    def test_a_failed_read_reports_failed_with_the_neutral_value(self) -> None:
        def boom() -> str:
            raise _StubReadError

        outcome = guarded_read("mr author", boom, neutral="")
        assert outcome.value == ""
        assert outcome.failed is True
        assert isinstance(outcome.error, _StubReadError)

    def test_a_failed_read_is_logged_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        def boom() -> int:
            raise _StubReadError

        with caplog.at_level(logging.WARNING, logger="teatree.cli.review.guarded_read"):
            guarded_read("inline draft count", boom, neutral=0)
        assert "inline draft count" in caplog.text
        assert _READ_FAILURE in caplog.text


class TestReadOrRefuse:
    """The no-safe-neutral variant: refuse rather than guess."""

    def test_a_successful_read_passes_through(self) -> None:
        assert read_or_refuse("base url", lambda: "https://example.test/api/v4") == "https://example.test/api/v4"

    def test_a_failed_read_refuses(self) -> None:
        def boom() -> str:
            raise _StubReadError

        with pytest.raises(ReadRefusedError, match="base url"):
            read_or_refuse("base url", boom)
