"""Behaviour tests for ``teatree.core.pr_create_verify`` — PR existence re-read (#1194)."""

from django.test import SimpleTestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.pr_create_verify import verify_pr_exists

_URL = "https://github.com/souliane/teatree/pull/7"


class _FakeHost:
    def __init__(self, state: PrOpenState | Exception) -> None:
        self._state = state
        self.calls: list[str] = []

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        self.calls.append(pr_url)
        if isinstance(self._state, Exception):
            raise self._state
        return self._state


class TestVerifyPrExists(SimpleTestCase):
    def test_open_pr_is_confirmed(self) -> None:
        host = _FakeHost(PrOpenState.OPEN)

        outcome = verify_pr_exists(host, _URL)

        assert outcome.confirmed is True
        assert host.calls == [_URL]

    def test_merged_and_closed_states_are_confirmed_existence(self) -> None:
        for state in (PrOpenState.MERGED, PrOpenState.CLOSED):
            assert verify_pr_exists(_FakeHost(state), _URL).confirmed is True

    def test_unknown_state_is_not_confirmed(self) -> None:
        outcome = verify_pr_exists(_FakeHost(PrOpenState.UNKNOWN), _URL)

        assert outcome.confirmed is False
        assert "create_pr" in outcome.reason

    def test_reread_raising_degrades_to_not_confirmed(self) -> None:
        outcome = verify_pr_exists(_FakeHost(RuntimeError("network")), _URL)

        assert outcome.confirmed is False
