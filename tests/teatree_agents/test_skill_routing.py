from unittest.mock import patch

import pytest

import teatree.agents.skill_routing as routing_mod
from teatree.agents.skill_routing import AmbiguousSkillRouteError, ConflictingHarnessRoutingError, resolve_skill_route
from teatree.config.agent_spawn import AgentRouteCandidate


def _routes(*models: str) -> tuple[AgentRouteCandidate, ...]:
    return tuple(AgentRouteCandidate("claude_sdk", model) for model in models)


def test_zero_route_lists_preserve_legacy_routing() -> None:
    assert resolve_skill_route({"code": None}, ["code"], phase_candidates=[]) is None


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
