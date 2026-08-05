"""Harness-neutral standing directives — the layer-1 contract (#4166 Phase 1).

The three directives, their cadences, their per-slot scope and delivery cost, the
``Prompt``-row override, and the ``{slot_id, cadence_seconds, text, scope,
wakes_session}`` read surface every harness consumes. Nothing here knows about
slash commands, hooks, or session markers — that is the adapter's layer, pinned
separately in ``tests/test_standing_directives_adapter.py``.
"""

import re
from unittest import mock

import pytest
from django.db.utils import OperationalError
from django.test import TestCase

from teatree.core.models import Mode, Prompt
from teatree.loop.preset_resolution import ActivePreset
from teatree.loop.standing_directives import (
    MAX_DIRECTIVE_CHARS,
    SCOPE_ATTENDED,
    SCOPE_ATTENDED_SINGLETON,
    STANDING_DIRECTIVES,
    StandingDirective,
    StandingDirectivePayload,
    _self_pump_paused,
    golden_rule_cadence_seconds,
    override_prompt_name,
    pr_board_cadence_seconds,
    resolve_standing_directives,
    self_woken_turns_per_hour,
    todo_consolidate_cadence_seconds,
)


def _text(slot_id: str) -> str:
    return next(d.default_text for d in STANDING_DIRECTIVES if d.slot_id == slot_id)


# ── the harness-vocabulary predicate (minor 5) ───────────────────────
#
# The old guard was a five-substring denylist that let bare `/loop` and
# `/t3:interactive` through on a trailing space and named no session marker. A
# predicate over the SHAPE of a slash token makes that whole class impossible,
# and the control corpus below is what proves the predicate can go red.

_SLASH_SHAPED_TOKEN = re.compile(r"(?<!\S)/[A-Za-z0-9][\w:.\-]*")

_HARNESS_TOKENS = (
    ".teatree-active",
    ".t3-engaged",
    "teatree-active",
    "t3-engaged",
    "directives-pending",
    "loop-pending",
    "session marker",
    "pretooluse",
    "userpromptsubmit",
    "sessionstart",
    "stop hook",
    "hook",
    "additionalcontext",
    "claude",
    "anthropic",
    "cursor",
    "copilot",
    "codex",
    "slash command",
)

#: Mutations that MUST be caught. The first is the reviewer's own — it slipped
#: past the shipped denylist, which is why the guard is a predicate now.
_HARNESS_VOCABULARY_MUTATIONS = (
    " Set the .teatree-active session marker.",
    " per the /t3:interactive workflow.",
    " register a /loop",
    " the UserPromptSubmit hook injects this.",
    " ask Claude to do it.",
)


def harness_vocabulary_violations(text: str) -> list[str]:
    """Every harness-specific token in *text* — empty means layer-1 neutral."""
    lowered = text.lower()
    found = {match.group(0) for match in _SLASH_SHAPED_TOKEN.finditer(text)}
    found.update(token for token in _HARNESS_TOKENS if token in lowered)
    return sorted(found)


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
        # Assert the BEHAVIOUR clause, never a pointer to a harness-specific
        # skill — the module claims to carry no such vocabulary.
        assert "never implements itself" in text
        assert "delegate every implementation" in text

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

    def test_the_slot_table_is_the_scope_and_delivery_cost_contract(self) -> None:
        # Cost follows the delivery shape: the zero-turn rule reaches every
        # attended session, and the only global slot is delivered once per host.
        by_slot = {d.slot_id: (d.scope, d.wakes_session) for d in STANDING_DIRECTIVES}

        assert by_slot == {
            "standing-golden-rule": (SCOPE_ATTENDED, False),
            "standing-todo-consolidate": (SCOPE_ATTENDED, True),
            "standing-pr-board": (SCOPE_ATTENDED_SINGLETON, True),
        }


class TestHarnessVocabularyIsAbsent:
    """Minor 5: the neutrality guard, and the control corpus proving it can fail."""

    @pytest.mark.parametrize("directive", STANDING_DIRECTIVES, ids=lambda d: d.slot_id)
    def test_a_real_directive_text_is_clean(self, directive: StandingDirective) -> None:
        assert harness_vocabulary_violations(directive.default_text) == []

    @pytest.mark.parametrize("mutation", _HARNESS_VOCABULARY_MUTATIONS)
    def test_control_a_harness_token_appended_to_a_real_text_is_caught(self, mutation: str) -> None:
        # CONTROL — each of these passed the shipped substring denylist. If any
        # returns clean, the guard is vacuous and its green means nothing.
        mutated = _text("standing-golden-rule") + mutation

        assert harness_vocabulary_violations(mutated) != []


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
        assert todo_consolidate_cadence_seconds() == 600
        assert pr_board_cadence_seconds() == 300

    def test_the_old_self_waking_floors_are_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The floor is the real bound, and the shipped ones permitted a single
        # session to wake itself ~100 times an hour. A configuration AT the old
        # floors must now be clamped up, not honoured.
        monkeypatch.setenv("T3_TODO_CONSOLIDATE_CADENCE", "300")
        monkeypatch.setenv("T3_PR_BOARD_CADENCE", "120")

        assert todo_consolidate_cadence_seconds() == 600
        assert pr_board_cadence_seconds() == 300

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
        assert [r.scope for r in resolved] == [SCOPE_ATTENDED, SCOPE_ATTENDED, SCOPE_ATTENDED_SINGLETON]
        assert [r.wakes_session for r in resolved] == [False, True, True]

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

    def test_as_dict_is_the_documented_five_key_contract(self) -> None:
        payload = resolve_standing_directives()[0].as_dict()

        # The declared TypedDict IS the contract, so the emitted payload's keys
        # must equal its annotations — not merely a hand-copied literal set.
        assert set(payload) == set(StandingDirectivePayload.__annotations__)
        assert payload["slot_id"] == "standing-golden-rule"
        assert payload["scope"] == "attended"
        assert payload["wakes_session"] is False


class TestTheSelfWokenTurnBudget(TestCase):
    """The aggregate cost, pinned — the number the cadences and floors exist to bound."""

    def test_the_default_budget(self) -> None:
        assert self_woken_turns_per_hour() == {"per_session": 2, "per_host_singleton": 6}

    def test_the_worst_case_budget_at_the_floors(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"T3_TODO_CONSOLIDATE_CADENCE": "1", "T3_PR_BOARD_CADENCE": "1"},
        ):
            assert self_woken_turns_per_hour() == {"per_session": 6, "per_host_singleton": 12}

    def test_a_disabled_slot_leaves_the_budget(self) -> None:
        Prompt.objects.create(name=override_prompt_name("standing-pr-board"), body="")

        assert self_woken_turns_per_hour() == {"per_session": 2, "per_host_singleton": 0}


class TestThePresetBrake(TestCase):
    """A self-waking directive IS a self-pump, so the away preset brakes it."""

    @staticmethod
    def _away_preset() -> ActivePreset:
        mode = Mode(name="holiday", defers_questions=True, pauses_self_pump=True)
        return ActivePreset(preset=mode, layer="override", reason="test", until=None)

    def test_a_paused_self_pump_drops_the_waking_slots_and_keeps_the_zero_turn_rule(self) -> None:
        with mock.patch("teatree.loop.standing_directives.resolve_active_preset", return_value=self._away_preset()):
            resolved = resolve_standing_directives()
            budget = self_woken_turns_per_hour()

        assert [r.slot_id for r in resolved] == ["standing-golden-rule"]
        assert budget == {"per_session": 0, "per_host_singleton": 0}

    def test_a_preset_that_does_not_pause_the_pump_delivers_everything(self) -> None:
        mode = Mode(name="reachable", defers_questions=False, pauses_self_pump=False)
        active = ActivePreset(preset=mode, layer="schedule", reason="test", until=None)

        with mock.patch("teatree.loop.standing_directives.resolve_active_preset", return_value=active):
            braked = _self_pump_paused()
            resolved = resolve_standing_directives()

        assert braked is False
        assert len(resolved) == len(STANDING_DIRECTIVES)

    def test_no_resolved_waking_slot_never_reads_the_preset(self) -> None:
        for directive in STANDING_DIRECTIVES:
            if directive.wakes_session:
                Prompt.objects.create(name=override_prompt_name(directive.slot_id), body="")

        with mock.patch("teatree.loop.standing_directives.resolve_active_preset") as resolver:
            resolved = resolve_standing_directives()

        assert [r.slot_id for r in resolved] == ["standing-golden-rule"]
        resolver.assert_not_called()

    def test_a_degraded_store_logs_no_traceback_on_this_silent_path(self) -> None:
        # The resolver's own fail-open WARNING carries exc_info, and the only
        # stderr this path has is a hook's, which the owner reads.
        degraded = mock.patch(
            "teatree.loop.preset_resolution._resolve_active_preset",
            side_effect=OperationalError("no such table: core_modeoverride"),
        )

        with degraded, self.assertNoLogs("teatree.loop.preset_resolution"):
            braked = _self_pump_paused()

        assert braked is False

    def test_a_raising_preset_resolver_fails_open_to_delivering(self) -> None:
        # Polarity: never suppress a rule because the brake could not be read.
        with mock.patch(
            "teatree.loop.standing_directives.resolve_active_preset",
            side_effect=RuntimeError("no preset table"),
        ):
            braked = _self_pump_paused()
            resolved = resolve_standing_directives()

        assert braked is False
        assert len(resolved) == len(STANDING_DIRECTIVES)
