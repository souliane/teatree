"""``teatree.loop.statusline_loops.cadence_for_loop`` — per-slot cadence resolver.

The single mapping from an infra-slot name to its cadence in seconds, shared
by the statusline next-tick countdown and ``t3 loop list`` (#1744). The drain
slot (#1744) is the branch under test here; the env-driven cadence readers it
delegates to are pinned so the resolved value is deterministic.
"""

import pytest

from teatree.loop.statusline_loops import _cadence_for_loop as cadence_for_loop


class TestCadenceForLoop:
    @pytest.mark.parametrize(
        ("env_var", "value", "slot", "expected"),
        [
            ("T3_QUEUE_DRAIN_CADENCE", "30", "loop-drain-queue", 30),
            ("T3_SLACK_ANSWER_CADENCE", "20", "loop-slack-answer", 20),
            ("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800", "loop-self-improve", 1800),
            ("T3_LOOP_OWNER_TTL", "1800", "loop-owner", 1800),
        ],
    )
    def test_named_slot_resolves_its_own_cadence(
        self, env_var: str, value: str, slot: str, expected: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(env_var, value)
        assert cadence_for_loop(slot) == expected

    def test_unknown_slot_falls_back_to_tick_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_LOOP_CADENCE", "600")
        assert cadence_for_loop("loop-tick") == 600
        assert cadence_for_loop("loop-something-new") == 600
