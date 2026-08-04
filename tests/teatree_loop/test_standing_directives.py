"""Harness-neutral standing directives — the layer-1 contract (#4166 Phase 1).

The three directives, their cadences, the ``Prompt``-row override, and the
``{slot_id, cadence_seconds, text, scope}`` read surface every harness consumes.
Nothing here knows about slash commands, hooks, or session markers — that is the
adapter's layer, pinned separately in ``tests/test_loop_registrations_hook.py``.
"""

from unittest import mock

import pytest
from django.test import TestCase

from teatree.core.models import Prompt
from teatree.loop.standing_directives import (
    MAX_DIRECTIVE_CHARS,
    STANDING_DIRECTIVE_SCOPE,
    STANDING_DIRECTIVES,
    StandingDirectivePayload,
    golden_rule_cadence_seconds,
    override_prompt_name,
    pr_board_cadence_seconds,
    resolve_standing_directives,
    todo_consolidate_cadence_seconds,
)


def _text(slot_id: str) -> str:
    return next(d.default_text for d in STANDING_DIRECTIVES if d.slot_id == slot_id)


class TestTheThreeSlots:
    def test_exactly_three_slots_in_order(self) -> None:
        assert [d.slot_id for d in STANDING_DIRECTIVES] == [
            "standing-golden-rule",
            "standing-todo-consolidate",
            "standing-pr-board",
        ]

    def test_golden_rule_covers_planning_and_the_orchestrate_only_boundary(self) -> None:
        text = _text("standing-golden-rule")
        assert "PLAN" in text
        for agent in ("t3:coder", "t3:debugger", "t3:tester", "t3:e2e"):
            assert agent in text
        assert "t3:planner" in text
        assert "skip-planning" in text
        assert "NOT a plan" in text
        # The second coupled failure: the orchestrator doing the work itself.
        assert "never implements itself" in text
        assert "/t3:interactive" in text

    def test_todo_directive_is_durable_state_first_with_a_conditional_rescan(self) -> None:
        text = _text("standing-todo-consolidate")
        assert "durable state FIRST" in text
        assert "ONLY if" in text
        assert "transcript" in text
        assert "outstanding user requests" in text

    def test_pr_board_directive_names_the_keystone_and_its_guards(self) -> None:
        text = _text("standing-pr-board")
        assert "ticket clear" in text
        assert "ticket merge" in text
        assert "LIVE head" in text
        assert "never a raw forge-CLI merge" in text
        assert "never merge over a hold" in text
        assert "maker ≠ checker" in text

    def test_every_default_text_is_within_the_context_cost_cap(self) -> None:
        for directive in STANDING_DIRECTIVES:
            assert len(directive.default_text) <= MAX_DIRECTIVE_CHARS, directive.slot_id

    def test_no_slash_loop_or_hook_vocabulary_leaks_into_layer_one(self) -> None:
        # Harness neutrality: another harness reads these texts verbatim, so no
        # Claude-plugin vocabulary may appear in them.
        for directive in STANDING_DIRECTIVES:
            lowered = directive.default_text.lower()
            for banned in ("/loop ", "pretooluse", "userpromptsubmit", "hook", "claude"):
                assert banned not in lowered, f"{directive.slot_id} leaks {banned!r}"


class TestCadences:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("T3_GOLDEN_RULE_CADENCE", "T3_TODO_CONSOLIDATE_CADENCE", "T3_PR_BOARD_CADENCE"):
            monkeypatch.delenv(name, raising=False)
        assert golden_rule_cadence_seconds() == 300
        assert todo_consolidate_cadence_seconds() == 1800
        assert pr_board_cadence_seconds() == 600

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_GOLDEN_RULE_CADENCE", "900")
        monkeypatch.setenv("T3_TODO_CONSOLIDATE_CADENCE", "3600")
        monkeypatch.setenv("T3_PR_BOARD_CADENCE", "1200")
        assert golden_rule_cadence_seconds() == 900
        assert todo_consolidate_cadence_seconds() == 3600
        assert pr_board_cadence_seconds() == 1200

    def test_floors_clamp_a_too_tight_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_GOLDEN_RULE_CADENCE", "1")
        monkeypatch.setenv("T3_TODO_CONSOLIDATE_CADENCE", "1")
        monkeypatch.setenv("T3_PR_BOARD_CADENCE", "1")
        assert golden_rule_cadence_seconds() == 60
        assert todo_consolidate_cadence_seconds() == 300
        assert pr_board_cadence_seconds() == 120

    @pytest.mark.parametrize("raw", ["", "   ", "not-a-number"])
    def test_garbage_override_degrades_to_the_default(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("T3_GOLDEN_RULE_CADENCE", raw)
        assert golden_rule_cadence_seconds() == 300


class TestResolveStandingDirectives(TestCase):
    """The read surface: compiled defaults, ``Prompt``-row override, fail-open."""

    def test_resolves_all_three_from_the_compiled_defaults(self) -> None:
        resolved = resolve_standing_directives()

        assert [r.slot_id for r in resolved] == [d.slot_id for d in STANDING_DIRECTIVES]
        assert [r.text for r in resolved] == [d.default_text for d in STANDING_DIRECTIVES]
        assert [r.cadence_seconds for r in resolved] == [300, 1800, 600]
        assert {r.scope for r in resolved} == {STANDING_DIRECTIVE_SCOPE}

    def test_prompt_row_override_wins_over_the_compiled_default(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="Owner-edited board rule.")

        by_slot = {r.slot_id: r.text for r in resolve_standing_directives()}

        assert by_slot["standing-pr-board"] == "Owner-edited board rule."
        assert by_slot["standing-golden-rule"] == _text("standing-golden-rule")

    def test_an_empty_override_switches_that_slot_off(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-todo-consolidate"), body="   ")

        assert [r.slot_id for r in resolve_standing_directives()] == [
            "standing-golden-rule",
            "standing-pr-board",
        ]

    def test_an_over_cap_override_falls_back_to_the_compiled_default(self) -> None:
        Prompt.objects.create(
            name=override_prompt_name("standing-golden-rule"),
            body="x" * (MAX_DIRECTIVE_CHARS + 1),
        )

        by_slot = {r.slot_id: r.text for r in resolve_standing_directives()}

        assert by_slot["standing-golden-rule"] == _text("standing-golden-rule")

    def test_an_unreachable_override_store_still_yields_the_compiled_defaults(self) -> None:
        with mock.patch(
            "teatree.loop.standing_directives._override_texts",
            side_effect=RuntimeError("no database"),
        ):
            resolved = resolve_standing_directives()

        assert [r.text for r in resolved] == [d.default_text for d in STANDING_DIRECTIVES]

    def test_as_dict_is_the_documented_four_key_contract(self) -> None:
        payload = resolve_standing_directives()[0].as_dict()

        # The declared TypedDict IS the contract, so the emitted payload's keys
        # must equal its annotations — not merely a hand-copied literal set.
        assert set(payload) == set(StandingDirectivePayload.__annotations__)
        assert payload["slot_id"] == "standing-golden-rule"
        assert payload["scope"] == "attended"
