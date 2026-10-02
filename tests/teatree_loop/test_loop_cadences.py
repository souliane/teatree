"""Tests for the reactive-slot seam in ``teatree.loop.loop_cadences`` (#2650).

The three always-on reactive infra loops (Slack-answer, self-improve,
drain-queue) have no DB ``Loop`` row: their sub-minute cadence cannot be a
minute-granular cron, so each is its OWN ``/loop`` on a *duration* cadence. This
seam is the single source of truth both ``t3 loop <slot> start`` and the
owner-session bootstrap (``hooks.scripts.loop_registrations``) read, so they can
never disagree on a reactive slot's cadence or run command.
"""

import pytest

from teatree.loop.loop_cadences import (
    REACTIVE_SLOTS,
    reactive_slot,
    reactive_slot_directives,
    slack_answer_cadence_seconds,
)


class TestSlackAnswerFallbackCadence:
    """The timer is the missed-wake fallback, so its default is 5m, not a tight spin."""

    def test_default_is_five_minutes_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_SLACK_ANSWER_CADENCE", raising=False)
        assert slack_answer_cadence_seconds() == 300

    def test_invalid_override_falls_back_to_five_minutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", "garbage")
        assert slack_answer_cadence_seconds() == 300

    def test_deliberate_low_override_is_still_honoured_above_the_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", "20")
        assert slack_answer_cadence_seconds() == 20

    def test_override_below_floor_is_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", "5")
        assert slack_answer_cadence_seconds() == 15


class TestReactiveSlotCadence:
    def test_sub_minute_cadence_uses_the_seconds_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", "20")
        assert reactive_slot("loop-slack-answer").cadence() == "20s"

    def test_minute_aligned_cadence_uses_the_minutes_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_QUEUE_DRAIN_CADENCE", "120")
        assert reactive_slot("loop-drain-queue").cadence() == "2m"

    def test_loop_directive_is_the_slash_command_the_owner_registers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800")
        assert (
            reactive_slot("loop-self-improve").loop_directive()
            == "/loop 30m Run `t3 loop self-improve run --tier cheap`."
        )


class TestReactiveSlotRegistry:
    def test_reactive_slot_directives_covers_all_three_slots(self) -> None:
        directives = reactive_slot_directives()
        assert len(directives) == len(REACTIVE_SLOTS) == 3
        assert all(directive.startswith("/loop ") for directive in directives)
        assert {slot.slot_id for slot in REACTIVE_SLOTS} == {
            "loop-slack-answer",
            "loop-self-improve",
            "loop-drain-queue",
        }

    def test_unknown_slot_id_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            reactive_slot("loop-does-not-exist")
