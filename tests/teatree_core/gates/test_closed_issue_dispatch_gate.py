"""Closed-issue dispatch gate: an implementing dispatch needs a live issue (#2663).

Symmetric must-refuse / must-allow, mirroring ``test_plan_dispatch_gate.py``: the
gate is useless if it only ever passes, and harmful if it refuses a reviewer, a
planner, or — the failure mode that would freeze the whole board — every dispatch
on the box the moment the forge stops answering.
"""

import logging
from unittest.mock import patch

from django.test import TestCase

from teatree.core.gates.closed_issue_dispatch_gate import (
    CLOSED_ISSUE_STATES,
    ISSUE_CLOSED_PREFIX,
    IssueOpenState,
    closed_issue_dispatch_refusal,
    closed_reason_from_payload,
    open_state_from_payload,
)
from teatree.core.gates.plan_dispatch_gate import IMPLEMENTING_PHASES
from teatree.core.models import Ticket

_URL = "https://github.com/souliane/teatree/issues/4045"


class _Host:
    def __init__(self, payload: object = None, *, raises: bool = False) -> None:
        self.payload = payload
        self.raises = raises
        self.calls: list[str] = []

    def get_issue(self, issue_url: str) -> object:
        self.calls.append(issue_url)
        if self.raises:
            msg = "connection timeout"
            raise RuntimeError(msg)
        return self.payload


class _Provider:
    def __init__(self, host: _Host | None) -> None:
        self.host = host

    def get_code_host_for_url(self, overlay: object, issue_url: str) -> _Host | None:
        _ = (overlay, issue_url)
        return self.host


def _ticket(**overrides: object) -> Ticket:
    fields: dict[str, object] = {"overlay": "acme", "role": Ticket.Role.AUTHOR, "issue_url": _URL}
    fields.update(overrides)
    return Ticket.objects.create(**fields)


def _refusal(ticket: Ticket, *, phase: str = "coding", host: _Host | None) -> str | None:
    with (
        patch("teatree.core.gates.closed_issue_dispatch_gate.get_backend_provider", return_value=_Provider(host)),
        patch("teatree.core.gates.closed_issue_dispatch_gate.get_overlay_for_ticket", return_value=object()),
    ):
        return closed_issue_dispatch_refusal(ticket, phase=phase)


class TestOpenStateFromPayload:
    """The pure classifier: only a payload that positively SAYS closed is CLOSED."""

    def test_closed_synonyms_are_closed(self) -> None:
        for state in ("closed", "Closed", "completed", "cancelled"):
            assert open_state_from_payload({"state": state}) is IssueOpenState.CLOSED

    def test_open_states_are_open(self) -> None:
        for state in ("open", "opened", "OPEN"):
            assert open_state_from_payload({"state": state}) is IssueOpenState.OPEN

    def test_unclassifiable_payloads_are_unknown(self) -> None:
        for payload in (None, "closed", [], {}, {"state": None}, {"state": 3}, {"error": "nope"}):
            assert open_state_from_payload(payload) is IssueOpenState.UNKNOWN

    def test_an_error_envelope_is_unknown_even_when_it_carries_a_state(self) -> None:
        # The forge's error shape wins: a state field beside an error is not an answer.
        assert open_state_from_payload({"error": "boom", "state": "closed"}) is IssueOpenState.UNKNOWN

    def test_closed_reason_is_read_when_present_and_blank_otherwise(self) -> None:
        assert closed_reason_from_payload({"state_reason": "not_planned"}) == "not_planned"
        assert closed_reason_from_payload({"state_reason": None}) == ""
        assert closed_reason_from_payload({}) == ""
        assert closed_reason_from_payload(None) == ""

    def test_the_scanner_reads_this_very_set(self) -> None:
        # One fact, two readers: the gate refuses exactly what the tick auto-ignores.
        # A scanner that re-listed the states could learn a fourth spelling the gate
        # had not, and would then ignore a ticket whose coder it still let run.
        from teatree.loop.scanners import ticket_dispositions  # noqa: PLC0415 — deferred: keeps loop out of a core test

        assert ticket_dispositions.CLOSED_ISSUE_STATES is CLOSED_ISSUE_STATES

    def test_every_closed_state_classifies_as_closed(self) -> None:
        for state in CLOSED_ISSUE_STATES:
            assert open_state_from_payload({"state": state}) is IssueOpenState.CLOSED


class TestRefusesAClosedIssueImplementingDispatch(TestCase):
    def test_every_implementing_phase_is_refused(self) -> None:
        for phase in sorted(IMPLEMENTING_PHASES):
            ticket = _ticket(issue_url=f"{_URL}/{phase}")
            refusal = _refusal(ticket, phase=phase, host=_Host({"state": "closed"}))
            assert refusal is not None, f"{phase} must be refused against a closed issue"
            assert refusal.startswith(ISSUE_CLOSED_PREFIX)

    def test_refusal_names_the_subagent_the_ticket_and_the_issue(self) -> None:
        ticket = _ticket()
        refusal = _refusal(ticket, phase="debugging", host=_Host({"state": "closed"}))
        assert refusal is not None
        assert "t3:debugger" in refusal
        assert str(ticket.pk) in refusal
        assert _URL in refusal

    def test_refusal_quotes_the_forges_own_close_reason(self) -> None:
        refusal = _refusal(_ticket(), host=_Host({"state": "closed", "state_reason": "not_planned"}))
        assert refusal is not None
        assert "not_planned" in refusal

    def test_refusal_names_both_remedies_and_the_salvage_step(self) -> None:
        ticket = _ticket()
        refusal = _refusal(ticket, host=_Host({"state": "closed"}))
        assert refusal is not None
        assert "reopen the issue" in refusal
        assert f"ticket transition {ticket.pk} ignore" in refusal
        assert "workspace salvage" in refusal


class TestLetsALiveDispatchThrough(TestCase):
    def test_an_open_issue_is_not_refused(self) -> None:
        assert _refusal(_ticket(), host=_Host({"state": "open"})) is None

    def test_non_implementing_phases_are_never_refused(self) -> None:
        for phase in ("planning", "reviewing", "shipping", "requesting_review", "retro"):
            host = _Host({"state": "closed"})
            assert _refusal(_ticket(issue_url=f"{_URL}/{phase}"), phase=phase, host=host) is None
            assert host.calls == [], f"{phase} must not pay a forge round trip"

    def test_a_reviewer_ticket_is_never_refused(self) -> None:
        host = _Host({"state": "closed"})
        assert _refusal(_ticket(role=Ticket.Role.REVIEWER), host=host) is None
        assert host.calls == []

    def test_a_synthetic_cadence_url_is_never_refused(self) -> None:
        host = _Host({"state": "closed"})
        assert _refusal(_ticket(issue_url="scanning-news://t3-teatree"), host=host) is None
        assert host.calls == []

    def test_an_empty_url_is_never_refused(self) -> None:
        host = _Host({"state": "closed"})
        assert _refusal(_ticket(issue_url=""), host=host) is None
        assert host.calls == []

    def test_a_ticket_already_known_remote_missing_is_never_refused(self) -> None:
        host = _Host({"state": "closed"})
        assert _refusal(_ticket(remote_missing=True), host=host) is None
        assert host.calls == []


class TestTheGateFailsOpen(TestCase):
    """Uncertainty resolves toward DISPATCHING — the opposite of the reopen reader.

    Acting wrongly here BLOCKS live work rather than reviving dead work, and the
    disposition scan retries every tick, so a forge outage must cost nothing.
    """

    def test_a_raising_host_is_not_refused_and_says_so(self) -> None:
        with self.assertLogs("teatree.core.gates.closed_issue_dispatch_gate", level=logging.WARNING) as logs:
            assert _refusal(_ticket(), host=_Host(raises=True)) is None
        assert any("letting the dispatch through" in line for line in logs.output)

    def test_an_unresolvable_host_is_not_refused(self) -> None:
        assert _refusal(_ticket(), host=None) is None

    def test_an_error_payload_is_not_refused(self) -> None:
        assert _refusal(_ticket(), host=_Host({"error": "Not a GitHub issue URL"})) is None

    def test_a_payload_with_no_state_is_not_refused(self) -> None:
        assert _refusal(_ticket(), host=_Host({"title": "something"})) is None
