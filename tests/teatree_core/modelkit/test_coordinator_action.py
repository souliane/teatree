"""The coordinator's action table is closed, and no row of it can cross one of the owner's four limits (#5685)."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from teatree.core.modelkit.coordinator_action import (
    ACTION_SPECS,
    ALLOWED_DESTINATIONS,
    ActionDestination,
    CoordinatorActionKind,
    action_refusal,
    card_refusal,
)
from teatree.core.modelkit.owner_decision import OWNER_KEPT_DECISIONS, OwnerDecision


class TestEveryKindIsBoundedByTheOwnersLimits:
    @pytest.mark.parametrize("kind", list(CoordinatorActionKind))
    def test_the_kind_has_a_row_that_crosses_no_kept_decision_and_lands_on_an_allowed_destination(
        self, kind: CoordinatorActionKind
    ) -> None:
        spec = ACTION_SPECS[kind]

        assert (spec.crosses & OWNER_KEPT_DECISIONS, spec.destination in ALLOWED_DESTINATIONS) == (frozenset(), True)

    @pytest.mark.parametrize("kind", list(CoordinatorActionKind))
    def test_the_kind_runs_with_exactly_its_declared_arguments(self, kind: CoordinatorActionKind) -> None:
        arguments = dict.fromkeys(ACTION_SPECS[kind].arguments, "x")

        assert action_refusal(kind, arguments) == ""


class TestTheRuntimeCheckRefuses:
    def test_a_declared_destination_outside_the_owner_boundary(self) -> None:
        kind = CoordinatorActionKind.QUEUE_TASK
        outside = replace(ACTION_SPECS[kind], destination=ActionDestination.COLLEAGUE_MESSAGE)

        with patch.dict(ACTION_SPECS, {kind: outside}):
            refusal = action_refusal(kind, {"ticket": "https://github.com/o/r/issues/1"})

        assert "outside the owner's thread" in refusal

    def test_an_unknown_kind(self) -> None:
        assert "unknown action kind" in action_refusal("send_dm_to_colleague", {})

    @pytest.mark.parametrize("extra", ["channel", "thread_ts", "to", "model", "token", "command"])
    def test_an_argument_the_kind_does_not_declare(self, extra: str) -> None:
        kind = next(iter(CoordinatorActionKind))
        arguments = {**dict.fromkeys(ACTION_SPECS[kind].arguments, "x"), extra: "x"}

        assert extra in action_refusal(kind, arguments)

    @pytest.mark.parametrize("decision", sorted(OWNER_KEPT_DECISIONS))
    def test_a_row_that_would_cross_a_kept_decision(self, decision: OwnerDecision) -> None:
        kind = next(iter(CoordinatorActionKind))
        crossing = replace(ACTION_SPECS[kind], crosses=frozenset({decision}))

        with patch.dict(ACTION_SPECS, {kind: crossing}):
            refusal = action_refusal(kind, dict.fromkeys(crossing.arguments, "x"))

        assert decision in refusal


class TestTheTableHoldsExactlyTheFourKinds:
    def test_the_kinds_are_the_four_the_owner_approved(self) -> None:
        assert {kind.value for kind in CoordinatorActionKind} == {
            "bind_thread",
            "queue_task",
            "settle_card",
            "message_peer",
        }

    def test_settle_card_takes_a_card_and_lands_on_the_owners_own_channel(self) -> None:
        spec = ACTION_SPECS[CoordinatorActionKind.SETTLE_CARD]

        assert (spec.arguments, spec.destination) == (frozenset({"card"}), ActionDestination.OWNER_THREAD)

    def test_message_peer_takes_a_ticket_and_text_and_lands_on_a_bound_ticket(self) -> None:
        spec = ACTION_SPECS[CoordinatorActionKind.MESSAGE_PEER]

        assert (spec.arguments, spec.destination) == (frozenset({"ticket", "text"}), ActionDestination.BOUND_TICKET)

    def test_a_colleague_destination_is_never_allowed(self) -> None:
        assert ActionDestination.COLLEAGUE_MESSAGE not in ALLOWED_DESTINATIONS

    def test_message_peer_planted_on_a_colleague_destination_is_refused(self) -> None:
        kind = CoordinatorActionKind.MESSAGE_PEER
        to_colleague = replace(ACTION_SPECS[kind], destination=ActionDestination.COLLEAGUE_MESSAGE)

        with patch.dict(ACTION_SPECS, {kind: to_colleague}):
            refusal = action_refusal(kind, {"ticket": "https://github.com/o/r/issues/1", "text": "hello"})

        assert "outside the owner's thread" in refusal


class TestACardTheOwnerKeepsIsNeverSettled:
    @pytest.mark.parametrize("decision", sorted(OWNER_KEPT_DECISIONS))
    def test_a_card_deciding_one_of_the_four_limits_is_refused_naming_the_limit(self, decision: OwnerDecision) -> None:
        assert card_refusal(decision) == f"settle_card would cross the owner's {decision} limit"

    def test_a_card_with_no_recorded_decision_kind_is_refused(self) -> None:
        assert card_refusal(None) == "settle_card needs a card with a recorded decision kind"

    @pytest.mark.parametrize("decision", [OwnerDecision.ARCHITECTURE, OwnerDecision.PRODUCT_SCOPE])
    def test_a_card_deciding_none_of_the_limits_is_not_refused_here(self, decision: OwnerDecision) -> None:
        assert card_refusal(decision) == ""
