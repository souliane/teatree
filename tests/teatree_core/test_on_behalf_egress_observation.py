import pytest

from teatree.core.egress_transport import EgressKind
from teatree.core.on_behalf_egress_observation import (
    EgressAttempt,
    EgressDestination,
    EgressOutcome,
    observe_egress,
    observe_egress_errors,
    observe_on_behalf_egress,
)


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
