"""teatree.loops.fleet_policy — owner-intake loops are never fleet-masked off (#3632).

The deploy entrypoint's fleet-role reseed force-OFFs its DISABLED set on every
deploy (a durable ``LoopState`` override that beats preset + base config). Owner-
intake loops — ``directive_loop`` (interprets the owner's captured directives),
``dispatch`` (posts deferred owner questions), ``inbox`` (ingests inbound DMs) —
must be pruned from that set, else the owner's intent is never even ingested. The
unit tests pin :func:`fleet_disable_set`; the integration test proves the end-to-
end effective verdict through the REAL preset/loop resolution
(:func:`teatree.loops.preset_status.effective_verdicts`), not a mock of it.
"""

import django.test

from teatree.core.models import Loop, LoopState, ModeOverride
from teatree.loops.fleet_policy import (
    DEFAULT_FLEET_DISABLED,
    DEFAULT_FLEET_ENABLED,
    OWNER_INTAKE_LOOPS,
    fleet_disable_set,
)
from teatree.loops.preset_seed import seed_default_presets_and_schedules
from teatree.loops.preset_status import effective_verdicts
from teatree.loops.seed import seed_default_loops_and_prompts


class TestFleetDisableSet:
    def test_prunes_owner_intake_loops(self) -> None:
        assert fleet_disable_set(["review", "directive_loop"], enabled=["inbox"]) == ["review"]

    def test_prunes_every_owner_intake_member(self) -> None:
        disabled = ["review", *sorted(OWNER_INTAKE_LOOPS)]
        assert fleet_disable_set(disabled, enabled=[]) == ["review"]

    def test_prunes_enabled_overlap(self) -> None:
        assert fleet_disable_set(["review", "tickets"], enabled=["tickets"]) == ["review"]

    def test_preserves_order_and_dedups(self) -> None:
        assert fleet_disable_set(["ship", "review", "ship"], enabled=[]) == ["ship", "review"]

    def test_default_disabled_carries_no_intake_loop(self) -> None:
        # The shipped default must already be intake-clean — a re-mask can't sneak
        # back in through the default itself.
        assert fleet_disable_set(DEFAULT_FLEET_DISABLED, enabled=DEFAULT_FLEET_ENABLED) == list(DEFAULT_FLEET_DISABLED)
        assert OWNER_INTAKE_LOOPS.isdisjoint(DEFAULT_FLEET_DISABLED)


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestUnattendedReseedAdmitsIntakeLoops(django.test.TestCase):
    """After the unattended reseed, intake loops ADMIT while review/followup stay masked."""

    def _apply_fleet_reseed(self, disabled: list[str], *, enabled: list[str]) -> None:
        """Model the entrypoint reseed: force-OFF the pruned DISABLED set."""
        for name in fleet_disable_set(disabled, enabled=enabled):
            LoopState.objects.override(name, on=False)

    def test_directive_and_dispatch_admit_while_review_and_followup_are_masked(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        # The operator opted `directive_loop` in (it ships disabled behind its flag);
        # under the unattended posture it must keep interpreting captured directives.
        Loop.objects.filter(name="directive_loop").update(enabled=True)
        ModeOverride.objects.set_override("unattended")

        # A box that lists the intake loops in its DISABLED config — the prune is
        # what keeps them runnable (anti-vacuous: without it directive_loop/dispatch
        # would be forced off and masked).
        self._apply_fleet_reseed(["review", "directive_loop", "dispatch"], enabled=["inbox"])

        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["directive_loop"].admitted is True
        assert verdicts["directive_loop"].layer != "forced"
        assert verdicts["dispatch"].admitted is True
        # review is forced off by the reseed (colleague-facing, stays masked)…
        assert verdicts["review"].admitted is False
        assert verdicts["review"].layer == "forced"
        # …and followup stays masked by the unattended preset itself.
        assert verdicts["followup"].admitted is False
