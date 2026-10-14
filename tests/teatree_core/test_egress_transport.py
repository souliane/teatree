"""teatree.core.egress_transport — the one seam a preview swaps the final transport at.

The whole no-post guarantee of ``t3 loops tick --loop followup --dry-run`` rests on this
leaf: every gate, route and audit above it still runs for real, and only the wire call is
replaced. So the properties pinned here are that it is INERT with no suppressor
installed (live is untouched), that the swap is scoped to the context that set it, and
that an installed observer hears every attempt's outcome.
"""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from teatree.core.egress_transport import (
    EgressAttempt,
    EgressDestination,
    EgressKind,
    EgressOutcome,
    observe_egress,
    observe_egress_errors,
    observe_on_behalf_egress,
    run_egress_transport,
    suppress_on_behalf_egress,
    suppressed_egress_response,
)
from teatree.types import RawAPIDict


def _publish() -> RawAPIDict:
    return {"ok": True, "ts": "live"}


def _suppressor(target: str, action: str, kind: EgressKind) -> RawAPIDict:
    return {"ok": True, "ts": f"{kind.value}:{action}:{target}"}


class TestNoSuppressorIsInert:
    def test_run_egress_transport_publishes(self) -> None:
        assert run_egress_transport("mr", "nag", EgressKind.POST, _publish) == {"ok": True, "ts": "live"}

    def test_suppressed_egress_response_is_none(self) -> None:
        assert suppressed_egress_response("mr", "nag", EgressKind.POST) is None


class TestSuppressorReplacesOnlyTheTransport:
    def test_publish_is_never_called(self) -> None:
        calls: list[int] = []

        def publish() -> RawAPIDict:
            calls.append(1)
            return {"ok": True, "ts": "live"}

        with suppress_on_behalf_egress(_suppressor):
            response = run_egress_transport("mr", "nag", EgressKind.REACTION, publish)

        assert calls == []
        assert response == {"ok": True, "ts": "reaction:nag:mr"}

    def test_the_swap_is_scoped_to_the_context_that_set_it(self) -> None:
        with suppress_on_behalf_egress(_suppressor):
            pass
        assert run_egress_transport("mr", "nag", EgressKind.POST, _publish) == {"ok": True, "ts": "live"}

    def test_suppressed_egress_response_returns_the_suppressor_result_directly(self) -> None:
        """Direct-call coverage of the scope-open branch, not only via ``run_egress_transport``."""
        with suppress_on_behalf_egress(_suppressor):
            response = suppressed_egress_response("mr", "nag", EgressKind.POST)

        assert response == {"ok": True, "ts": "post:nag:mr"}

    def test_a_copied_context_carries_the_suppressor_into_a_worker_thread(self) -> None:
        """The followup preview's pool submits each job through ``copy_context().run`` for exactly this."""
        with suppress_on_behalf_egress(_suppressor), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, run_egress_transport, "mr", "nag", EgressKind.POST, _publish)
            response = future.result(timeout=5)

        assert response == {"ok": True, "ts": "post:nag:mr"}

    def test_a_bare_worker_thread_still_publishes_live(self) -> None:
        with suppress_on_behalf_egress(_suppressor), ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(run_egress_transport, "mr", "nag", EgressKind.POST, _publish).result(timeout=5)

        assert response == {"ok": True, "ts": "live"}


def test_observer_reports_success_and_refusal_and_restores_nested_context() -> None:
    outer: list[EgressAttempt] = []
    inner: list[EgressAttempt] = []
    destination = EgressDestination(channel="C123", thread="1.1")
    with observe_on_behalf_egress(outer.append):
        observe_egress("C123", "post", EgressKind.POST, {"ok": True}, destination=destination)
        with observe_on_behalf_egress(inner.append):
            observe_egress("C123", "post", EgressKind.POST, {"ok": False, "error": "denied"})
        reason = "wire failed"
        with (
            pytest.raises(RuntimeError, match="wire failed"),
            observe_egress_errors("C123", "update", EgressKind.UPDATE, destination=destination),
        ):
            raise RuntimeError(reason)
    assert [event.outcome for event in outer] == [EgressOutcome.POSTED, EgressOutcome.REFUSED]
    assert outer[0].channel == "C123"
    assert outer[0].thread == "1.1"
    assert outer[1].reason == "wire failed"
    assert [event.reason for event in inner] == ["denied"]
