from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness_dispatch as harness_dispatch_mod
import teatree.agents.skill_routing as routing_mod
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.skill_routing import (
    QUOTA_AUTH_HOLD,
    AmbiguousSkillRouteError,
    ConflictingHarnessRoutingError,
    clear_route_availability_cache,
    resolve_skill_route,
)
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.modelkit.phases import KNOWN_PHASES
from teatree.core.models import AgentRouteAvailability
from teatree.types import SkillMetadata


def _routes(*models: str) -> tuple[AgentRouteCandidate, ...]:
    return tuple(AgentRouteCandidate("claude_sdk", model) for model in models)


def test_zero_route_lists_preserve_legacy_routing() -> None:
    assert resolve_skill_route({"code": None}, ["code"], phase_candidates=[]) is None


def test_runtime_fallback_requires_a_failure_signal_not_narration() -> None:
    assert routing_mod.runtime_fallback_reason("the process finished") is None
    assert routing_mod.runtime_fallback_reason("failed to start") == "failed to start"
    assert routing_mod.runtime_fallback_reason("spawn x ENOENT") == "spawn x ENOENT"
    assert routing_mod.runtime_fallback_reason("agent could not be spawned: E2BIG") is None
    assert routing_mod.runtime_fallback_reason("ProcessError: the agent process exited with code 1") is None
    assert routing_mod.runtime_fallback_reason("Failed to spawn: pytest") is None


def test_one_loaded_skill_owns_ordered_route() -> None:
    route = _routes("opus", "sonnet")

    assert resolve_skill_route({"code": route}, ["code", "rules"], phase_candidates=[]) == (
        "code",
        route,
    )


def test_multiple_loaded_route_owners_fail_deterministically() -> None:
    with pytest.raises(AmbiguousSkillRouteError, match=r"code, review"):
        resolve_skill_route(
            {"review": _routes("sonnet"), "code": _routes("opus")},
            ["review", "code"],
            phase_candidates=[],
        )


def test_primary_lifecycle_skill_owns_route_over_routed_companions() -> None:
    review = _routes("opus")

    assert resolve_skill_route(
        {"review": review, "code": _routes("sonnet")},
        ["rules", "review", "code"],
        phase_candidates=[],
        primary_skill="review",
    ) == ("review", review)


def test_skill_route_and_phase_candidates_have_one_owner() -> None:
    with pytest.raises(ConflictingHarnessRoutingError, match="factory_phase_harness_candidates"):
        resolve_skill_route(
            {"code": _routes("opus")},
            ["code"],
            phase_candidates=["claude_sdk"],
        )


def test_runtime_fallback_taxonomy_is_strict() -> None:
    eligible = [
        "401 authentication failed",
        "429 quota exhausted",
        "model access denied",
        "connection timed out",
        "503 provider unavailable",
    ]
    ineligible = ["invalid output schema", "permission denied by tool policy", "assertion failed"]

    assert all(routing_mod.runtime_fallback_reason(message) for message in eligible)
    assert all(routing_mod.runtime_fallback_reason(message) is None for message in ineligible)


def test_cached_unavailable_lane_does_not_probe_again() -> None:
    routing_mod.clear_route_availability_cache()
    candidate = AgentRouteCandidate("codex_app_server", "gpt-5.6-codex")
    with (
        patch.object(routing_mod, "_persistent_observation", return_value=None) as persistent,
        patch.object(routing_mod, "_persist"),
    ):
        calls = 0

        def probe() -> str | None:
            nonlocal calls
            calls += 1
            return "managed ChatGPT login unavailable"

        assert routing_mod.cached_unavailable_reason("teatree", candidate, probe) is not None
        assert routing_mod.cached_unavailable_reason("teatree", candidate, probe) is not None

    assert calls == 1
    persistent.assert_called_once()


def test_new_runtime_observation_replaces_cached_availability() -> None:
    routing_mod.clear_route_availability_cache()
    candidate = AgentRouteCandidate("codex_app_server", "gpt-5.6-codex")
    with (
        patch.object(routing_mod, "_persistent_observation", return_value=None),
        patch.object(routing_mod, "_persist"),
    ):
        assert routing_mod.cached_unavailable_reason("teatree", candidate, lambda: None) is None
        routing_mod.record_route_unavailable("teatree", candidate, "quota exhausted")

        assert routing_mod.cached_unavailable_reason("teatree", candidate, lambda: None) == "quota exhausted"


def test_cached_healthy_lane_still_checks_this_workers_local_capability() -> None:
    routing_mod.clear_route_availability_cache()
    candidate = AgentRouteCandidate("codex_app_server", "gpt-5.6-codex")
    with (
        patch.object(routing_mod, "_persistent_observation", return_value=None),
        patch.object(routing_mod, "_persist"),
    ):
        assert routing_mod.cached_unavailable_reason("teatree", candidate, lambda: None) is None
        assert (
            routing_mod.cached_unavailable_reason(
                "teatree",
                candidate,
                lambda: "codex CLI is not installed or not on PATH",
            )
            == "codex CLI is not installed or not on PATH"
        )


def test_availability_observations_are_isolated_by_normalized_phase() -> None:
    routing_mod.clear_route_availability_cache()
    candidate = AgentRouteCandidate("codex_app_server", "gpt-5.6-codex")
    with (
        patch.object(routing_mod, "_persistent_observation", return_value=None),
        patch.object(routing_mod, "_persist"),
    ):
        assert (
            routing_mod.cached_unavailable_reason(
                "teatree",
                candidate,
                lambda: "phase policy denies shell",
                phase="scoping",
            )
            == "phase policy denies shell"
        )
        assert (
            routing_mod.cached_unavailable_reason(
                "teatree",
                candidate,
                lambda: None,
                phase="code",
            )
            is None
        )
        assert (
            routing_mod.cached_unavailable_reason(
                "teatree",
                candidate,
                lambda: None,
                phase="coding",
            )
            is None
        )


_ROW = (AgentRouteCandidate("codex_app_server", "gpt-6-sol"), AgentRouteCandidate("claude_sdk", "claude-opus-5-5"))
_BOX_ROUTES = {
    "code": _ROW,
    "debug": _ROW,
    "test": _ROW,
    "e2e-review": (AgentRouteCandidate("claude_sdk", "claude-opus-5-5"),),
}


class TestTheArchitectureDesignRoute(TestCase):
    """The planning route row moves only the phases whose own lifecycle skill is architecture-design."""

    def _route_sources(self, skill_models: dict[str, tuple[AgentRouteCandidate, ...]]) -> dict[str, str]:
        sources: dict[str, str] = {}
        with patch.object(
            harness_dispatch_mod, "resolve_agent_config", return_value=AgentConfig(skill_models=skill_models)
        ):
            for phase in sorted(KNOWN_PHASES):
                skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=SkillMetadata())
                sources[phase] = resolve_dispatch_harness(phase=phase, skills=skills).route_source_skill
        return sources

    def test_a_lone_route_owner_never_builds_the_skill_index(self) -> None:
        clear_route_availability_cache()
        with patch.object(harness_dispatch_mod, "build_skill_index", side_effect=AssertionError("built")) as built:
            self._route_sources({"code": _BOX_ROUTES["code"]})

        built.assert_not_called()

    def test_architecture_design_route_moves_only_its_primary_phases(self) -> None:
        clear_route_availability_cache()
        before = self._route_sources(_BOX_ROUTES)
        after = self._route_sources({**_BOX_ROUTES, "architecture-design": _ROW})

        moved = {phase for phase in before if before[phase] != after[phase]}
        assert moved == {"planning", "directive_interpreting"}
        assert {after[phase] for phase in moved} == {"architecture-design"}


class TestDurableAvailability(TestCase):
    def test_a_healthy_probe_after_a_hold_expires_removes_its_durable_row(self) -> None:
        clear_route_availability_cache()
        self.addCleanup(clear_route_availability_cache)
        candidate = AgentRouteCandidate("codex_app_server", "gpt-5.6-codex")
        routing_mod.record_route_unavailable("teatree", candidate, "auth failed", retry_after=QUOTA_AUTH_HOLD)
        assert AgentRouteAvailability.objects.filter(harness="codex_app_server").exists()

        with patch("django.utils.timezone.now", return_value=timezone.now() + QUOTA_AUTH_HOLD + timedelta(minutes=1)):
            assert routing_mod.cached_unavailable_reason("teatree", candidate, lambda: None) is None

        assert not AgentRouteAvailability.objects.filter(harness="codex_app_server").exists()


def test_an_owner_another_owner_requires_yields_to_the_more_specific_one() -> None:
    index = [{"skill": "review", "requires": ["code"]}, {"skill": "code", "requires": []}]
    route = _routes("opus")

    assert resolve_skill_route(
        {"review": route, "code": _routes("sonnet")}, ["review", "code"], phase_candidates=[], skill_index=lambda: index
    ) == ("review", route)
