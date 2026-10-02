"""Decision core for the cron-shells-a-t3-loop gate (#2663 dream gap)."""

import pytest

from teatree.core.gates.cron_loop_shell_gate import (
    REACTIVE_RUN_SLOTS,
    CronLoopShellFinding,
    cron_loop_ok_reason,
    deny_reason,
    find_cron_loop_shell,
)
from teatree.loop.loop_cadences import REACTIVE_SLOTS

_TICK_PROMPTS = [
    "Run `t3 loops tick --loop dispatch` in Bash, then briefly report the tick summary.",
    "t3 loops tick --loop review",
    "t3 loop tick",
    "!t3 loops tick",
]

_REACTIVE_PROMPTS = [
    "/loop 30s Run `t3 loop drain-queue run`.",
    "Run `t3 loop slack-answer run`.",
    "t3 loop self-improve run --tier cheap",
]

_INNOCENT_PROMPTS = [
    "/followup",
    "Run `t3 worker status` and report whether the fleet is alive.",
    "Check the backlog and pick the next ticket.",
    "t3 loop status",
    "t3 loop enable dispatch",
    "",
]


class TestTickShape:
    @pytest.mark.parametrize("prompt", _TICK_PROMPTS)
    @pytest.mark.parametrize("worker_alive", [True, False])
    def test_tick_is_refused_regardless_of_worker_state(self, prompt: str, *, worker_alive: bool) -> None:
        finding = find_cron_loop_shell(prompt, worker_alive=worker_alive)

        assert finding is not None
        assert finding.shape == "tick"


class TestReactiveShape:
    @pytest.mark.parametrize("prompt", _REACTIVE_PROMPTS)
    def test_reactive_is_refused_while_the_worker_drives_it(self, prompt: str) -> None:
        finding = find_cron_loop_shell(prompt, worker_alive=True)

        assert finding is not None
        assert finding.shape == "reactive"

    @pytest.mark.parametrize("prompt", _REACTIVE_PROMPTS)
    def test_reactive_is_allowed_when_no_worker_is_alive(self, prompt: str) -> None:
        assert find_cron_loop_shell(prompt, worker_alive=False) is None


class TestInnocentPrompts:
    @pytest.mark.parametrize("prompt", _INNOCENT_PROMPTS)
    @pytest.mark.parametrize("worker_alive", [True, False])
    def test_unrelated_prompt_is_never_a_finding(self, prompt: str, *, worker_alive: bool) -> None:
        assert find_cron_loop_shell(prompt, worker_alive=worker_alive) is None


class TestSlotParity:
    def test_declared_slots_match_the_reactive_slot_registry(self) -> None:
        # The core cannot import teatree.loop (backwards edge), so the slot names are
        # vendored — this pins them against the registry so a rename cannot drift.
        registry = {slot.slot_id.removeprefix("loop-") for slot in REACTIVE_SLOTS}

        assert set(REACTIVE_RUN_SLOTS) == registry

    def test_every_registry_run_command_is_refused_while_the_worker_is_alive(self) -> None:
        for slot in REACTIVE_SLOTS:
            finding = find_cron_loop_shell(slot.loop_directive(), worker_alive=True)
            assert finding is not None, slot.slot_id
            assert finding.shape == "reactive"


class TestDenyReason:
    def test_tick_reason_names_the_worker_and_the_normal_way(self) -> None:
        finding = find_cron_loop_shell("t3 loops tick --loop dispatch", worker_alive=True)
        assert finding is not None

        reason = deny_reason(finding)

        assert "t3 worker" in reason
        assert "t3 loop enable" in reason
        assert "cron-loop-ok" in reason

    def test_reactive_reason_names_the_slot_command(self) -> None:
        finding = find_cron_loop_shell("t3 loop drain-queue run", worker_alive=True)
        assert finding is not None

        assert "t3 loop drain-queue run" in deny_reason(finding)


class TestOkToken:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[cron-loop-ok: pre-flip box, worker is down]", "pre-flip box, worker is down"),
            ("prefix [cron-loop-ok: reason] suffix", "reason"),
            ("[cron-loop-ok:   spaced   ]", "spaced"),
            ("[cron-loop-ok: ]", None),
            ("no token here", None),
            ("", None),
        ],
    )
    def test_token_extraction(self, text: str, expected: str | None) -> None:
        assert cron_loop_ok_reason(text) == expected

    def test_token_past_the_scan_window_is_ignored(self) -> None:
        assert cron_loop_ok_reason("x" * 600 + "[cron-loop-ok: late]") is None


class TestFindingShape:
    def test_finding_carries_the_matched_command(self) -> None:
        finding = find_cron_loop_shell("/loop 30s Run `t3 loop drain-queue run`.", worker_alive=True)

        assert isinstance(finding, CronLoopShellFinding)
        assert finding.command == "t3 loop drain-queue run"
