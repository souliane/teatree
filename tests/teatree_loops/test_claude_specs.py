"""Map enabled DB ``Loop`` rows → native Claude ``/loop`` specs (#2650).

The seam the owner-session hook and the ``/t3:loops`` enable/disable skill both
read: each ENABLED :class:`Loop` row mirrors to EXACTLY one native Claude
``/loop`` (per-loop, not per-group), with a STABLE ``slot_id`` per loop name so a
disable targets the exact cron to delete. Integration-first against the real DB.
"""

import datetime as dt

import django.test

from teatree.core.models import Loop, Prompt
from teatree.loops.claude_specs import (
    ClaudeLoopSpec,
    claude_loop_spec,
    cron_for_loop,
    enabled_loop_specs,
    loop_run_prompt,
    loop_slot_id,
)


def _prompt(name: str = "demo-prompt") -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


class TestSlotId:
    def test_slot_id_is_stable_and_namespaced_per_loop_name(self) -> None:
        assert loop_slot_id("inbox") == "t3-loop-inbox"
        assert loop_slot_id("inbox") == loop_slot_id("inbox")
        assert loop_slot_id("dream") != loop_slot_id("inbox")


class TestRunPrompt:
    def test_run_prompt_runs_only_that_loop_by_name(self) -> None:
        prompt = loop_run_prompt("dream")
        assert "t3 loops tick --loop dream" in prompt
        # The loop name is the per-loop discriminator a CronList match keys on.
        assert "dream" in prompt

    def test_command_token_is_backtick_terminated_so_a_name_prefix_does_not_collide(self) -> None:
        # A disable matches the BACKTICK-TERMINATED `--loop <name>` token, so a
        # name that is a strict PREFIX of another never deletes the wrong cron
        # (`ship` vs `ship-fast`). Non-vacuous: the bare substring DOES over-match,
        # which is exactly why the closing backtick is load-bearing.
        ship = loop_run_prompt("ship")
        ship_fast = loop_run_prompt("ship-fast")
        exact_token = "`t3 loops tick --loop ship`"
        assert exact_token in ship
        assert exact_token not in ship_fast  # the trailing backtick stops the prefix collision
        assert "t3 loops tick --loop ship" in ship_fast  # the bare substring WOULD over-match (the trap)


class TestCronForLoop:
    def test_minute_interval_under_an_hour(self) -> None:
        assert cron_for_loop(Loop(name="x", delay_seconds=60, script="s")) == "*/1 * * * *"
        assert cron_for_loop(Loop(name="x", delay_seconds=300, script="s")) == "*/5 * * * *"
        assert cron_for_loop(Loop(name="x", delay_seconds=1800, script="s")) == "*/30 * * * *"

    def test_hourly_and_multi_hour_intervals(self) -> None:
        assert cron_for_loop(Loop(name="x", delay_seconds=3600, script="s")) == "0 * * * *"
        assert cron_for_loop(Loop(name="x", delay_seconds=10800, script="s")) == "0 */3 * * *"

    def test_day_or_longer_interval_collapses_to_midnight_daily(self) -> None:
        assert cron_for_loop(Loop(name="x", delay_seconds=86400, script="s")) == "0 0 * * *"

    def test_daily_at_wall_clock_overrides_interval(self) -> None:
        loop = Loop(name="x", delay_seconds=86400, daily_at=dt.time(8, 0), script="s")
        assert cron_for_loop(loop) == "0 8 * * *"
        loop = Loop(name="x", delay_seconds=86400, daily_at=dt.time(3, 30), script="s")
        assert cron_for_loop(loop) == "30 3 * * *"

    def test_cadence_less_loop_runs_every_minute(self) -> None:
        assert cron_for_loop(Loop(name="x", delay_seconds=None, script="")) == "* * * * *"


class TestClaudeLoopSpec:
    def test_spec_combines_slot_id_cron_and_prompt(self) -> None:
        loop = Loop(name="ship", delay_seconds=300, script="s")
        spec = claude_loop_spec(loop)
        assert spec == ClaudeLoopSpec(
            slot_id="t3-loop-ship",
            cron="*/5 * * * *",
            prompt=loop_run_prompt("ship"),
        )


class TestEnabledLoopSpecs(django.test.TestCase):
    def test_maps_only_enabled_rows_one_spec_per_loop(self) -> None:
        Loop.objects.create(name="z-on", delay_seconds=300, prompt=_prompt(), enabled=True)
        Loop.objects.create(name="a-on", delay_seconds=60, prompt=_prompt(), enabled=True)
        Loop.objects.create(name="m-off", delay_seconds=300, prompt=_prompt(), enabled=False)

        specs = enabled_loop_specs()
        names = [s.slot_id for s in specs]

        assert names == ["t3-loop-a-on", "t3-loop-z-on"]  # enabled only, name-ordered
        assert all(isinstance(s, ClaudeLoopSpec) for s in specs)

    def test_each_spec_carries_that_rows_cron_and_run_command(self) -> None:
        Loop.objects.create(name="solo", delay_seconds=3600, prompt=_prompt(), enabled=True)
        (spec,) = enabled_loop_specs()
        assert spec.slot_id == "t3-loop-solo"
        assert spec.cron == "0 * * * *"
        assert "t3 loops tick --loop solo" in spec.prompt

    def test_no_enabled_rows_yields_no_specs(self) -> None:
        Loop.objects.create(name="off", delay_seconds=60, prompt=_prompt(), enabled=False)
        assert enabled_loop_specs() == []
