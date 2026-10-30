"""The coordinator's action table is closed, and no row of it can cross one of the owner's four limits (#5685)."""

from unittest.mock import patch

import pytest

from teatree.core.modelkit.coordinator_action import (
    ACTION_SPECS,
    ALLOWED_DESTINATIONS,
    ActionDestination,
    ActionSpec,
    CoordinatorActionKind,
    action_refusal,
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
        outside = ActionSpec(
            destination=ActionDestination.COLLEAGUE_MESSAGE,
            crosses=frozenset(),
            arguments=ACTION_SPECS[kind].arguments,
        )

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
        crossing = ActionSpec(
            destination=ACTION_SPECS[kind].destination,
            crosses=frozenset({decision}),
            arguments=ACTION_SPECS[kind].arguments,
        )

        with patch.dict(ACTION_SPECS, {kind: crossing}):
            refusal = action_refusal(kind, dict.fromkeys(crossing.arguments, "x"))

        assert decision in refusal
