"""Tests for teatree.core.verify_by_reread."""

from teatree.core.verify_by_reread import RereadOutcome, verify_by_reread


class TestRereadOutcome:
    def test_confirmed_ok_carries_no_reason(self) -> None:
        outcome = RereadOutcome.confirmed_ok()
        assert outcome.confirmed is True
        assert outcome.reason == ""

    def test_not_confirmed_carries_the_reason(self) -> None:
        outcome = RereadOutcome.not_confirmed("message not found")
        assert outcome.confirmed is False
        assert outcome.reason == "message not found"


class TestVerifyByReread:
    def test_confirmed_when_reread_observes_true(self) -> None:
        outcome = verify_by_reread(label="thing", reread=lambda: True)
        assert outcome == RereadOutcome.confirmed_ok()

    def test_not_confirmed_when_reread_observes_false(self) -> None:
        outcome = verify_by_reread(label="thing", reread=lambda: False)
        assert outcome.confirmed is False
        assert "thing" in outcome.reason
        assert "did not observe" in outcome.reason

    def test_not_confirmed_when_reread_raises(self) -> None:
        def _boom() -> bool:
            msg = "transport exploded"
            raise RuntimeError(msg)

        outcome = verify_by_reread(label="thing", reread=_boom)
        assert outcome.confirmed is False
        assert "transport exploded" in outcome.reason

    def test_reread_exception_never_propagates(self) -> None:
        def _boom() -> bool:
            raise ValueError

        # Must not raise — a broken reread degrades to not_confirmed.
        outcome = verify_by_reread(label="thing", reread=_boom)
        assert outcome.confirmed is False
