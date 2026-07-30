"""Capacity-exhaustion ``fallback_model`` — the ladder resolver + the Lane-A wiring.

``resolve_fallback_model`` walks the tier catalog down one rung so a spawn degrades
to a less-exhausted pool for a turn instead of parking; ``_build_options`` pins the
resolved fallback on ``ClaudeAgentOptions.fallback_model`` (a Lane-A / ``claude_sdk``
win, independent of Lane B).
"""

from django.test import TestCase

from teatree.agents._headless_options import _build_options
from teatree.agents.model_tiering import TIER_MODELS, resolve_fallback_model, resolve_tier
from teatree.core.models import Session, Task, Ticket


class TestResolveFallbackModel:
    def test_frontier_degrades_to_balanced(self) -> None:
        assert resolve_fallback_model(TIER_MODELS["frontier"]) == resolve_tier("balanced")

    def test_balanced_degrades_to_cheap(self) -> None:
        assert resolve_fallback_model(TIER_MODELS["balanced"]) == resolve_tier("cheap")

    def test_cheapest_rung_has_no_fallback(self) -> None:
        assert resolve_fallback_model(TIER_MODELS["cheap"]) is None

    def test_family_short_name_is_recognised(self) -> None:
        # A bare family short-name (not the dated id) still resolves its tier.
        assert resolve_fallback_model("opus") == resolve_tier("balanced")

    def test_unknown_pin_gets_no_guessed_fallback(self) -> None:
        assert resolve_fallback_model("vendor/some-model") is None

    def test_inherited_default_has_no_fallback(self) -> None:
        assert resolve_fallback_model(None) is None


class TestBuildOptionsFallbackWiring(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options_for(self, phase: str):
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        return _build_options(task, "ctx", phase=phase, skills=[])

    def test_frontier_phase_pins_the_balanced_fallback(self) -> None:
        # coding resolves to the frontier tier, so its exhaustion fallback is the
        # next-cheaper rung (balanced) — driven by the catalog, never hardcoded.
        options = self._options_for("coding")
        assert options.fallback_model == resolve_tier("balanced")
        assert options.fallback_model != options.model
