"""End-to-end dispatch proofs for ordered per-skill harness routes."""

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness_dispatch as harness_dispatch_mod
import teatree.agents.runner as runner_mod
from teatree.agents import harness_registry
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessFallbackError,
    HarnessFallbackKind,
    HarnessSpec,
    register_harness,
)
from teatree.agents.runner import TaskUsage, run_agent
from teatree.agents.session_lineage import resume_session_id
from teatree.agents.skill_routing import clear_route_availability_cache
from teatree.config import AgentHarnessProvider
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import Session, Task, TaskAttempt, UsageWindowState
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream

_CODEX_THREAD_ID = "22222222-2222-4222-8222-222222222222"


class _TypedFailureSession(FakeHarnessSession):
    async def query(self, prompt: str) -> None:
        del prompt
        message = "ChatGPT quota exhausted"
        raise HarnessFallbackError(message, kind=HarnessFallbackKind.QUOTA)


class _ProtocolFailureSession(FakeHarnessSession):
    async def query(self, prompt: str) -> None:
        del prompt
        message = "invalid app-server response schema"
        raise RuntimeError(message)


class _PostSideEffectFailureSession(FakeHarnessSession):
    async def query(self, prompt: str) -> None:
        del prompt
        message = "transport disconnected after tool execution"
        raise HarnessFallbackError(
            message,
            kind=HarnessFallbackKind.TRANSPORT,
            side_effects_started=True,
            agent_session_id=_CODEX_THREAD_ID,
        )


class _Harness:
    def __init__(self, session: type[FakeHarnessSession], capabilities: HarnessCapabilities) -> None:
        self._session = session
        self.capabilities = capabilities

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncIterator[FakeHarnessSession]:
        del options
        yield self._session(success_stream({"summary": "fallback done"}))


class TestSkillRouteRuntimeFallback(TestCase):
    def setUp(self) -> None:
        clear_route_availability_cache()
        self.built: list[str] = []

    def tearDown(self) -> None:
        for name in ("codex_like", "claude_like"):
            harness_registry._REGISTRY.pop(name, None)
        clear_route_availability_cache()

    def _register(
        self,
        name: str,
        session: type[FakeHarnessSession],
        *,
        spawns_cli_child: bool = False,
    ) -> None:
        capabilities = HarnessCapabilities(
            managed_lane=name == "codex_like",
            spawns_cli_child=spawns_cli_child,
        )

        def build(context: HarnessBuildContext) -> _Harness:
            del context
            self.built.append(name)
            return _Harness(session, capabilities)

        register_harness(HarnessSpec(name=name, factory=build, capabilities=capabilities, allows_provider=False))

    @staticmethod
    def _config(*names: str) -> AgentConfig:
        return AgentConfig(
            skill_models={
                "route-skill": tuple(AgentRouteCandidate(name, f"{name}-model") for name in names),
            }
        )

    @staticmethod
    def _task() -> Task:
        ticket = planned_ticket()
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))

    def _run(self, config: AgentConfig, *, skills: list[str] | None = None) -> TaskAttempt:
        task = self._task()
        with (
            patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config),
            patch.object(runner_mod, "resolve_skill_bundle", return_value=skills or ["route-skill"]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            return run_agent(task, phase="debugging", overlay_skill_metadata=SkillMetadata())

    def test_typed_codex_runtime_failure_switches_to_claude_and_records_provenance(self) -> None:
        self._register("codex_like", _TypedFailureSession)
        self._register("claude_like", FakeHarnessSession)

        final = self._run(self._config("codex_like", "claude_like"))

        attempts = list(final.task.attempts.order_by("pk"))
        assert self.built == ["codex_like", "claude_like"]
        assert len(attempts) == 2
        assert attempts[0].selected_harness == "codex_like"
        assert attempts[0].selected_provider == "managed_chatgpt"
        assert attempts[1].selected_harness == "claude_like"
        assert attempts[1].route_candidate_index == 1
        assert attempts[1].fallback_from_attempt == attempts[0]
        assert attempts[1].fallback_reason == "ChatGPT quota exhausted"
        assert final.exit_code == 0

    def test_selected_candidate_effort_reaches_spawn_and_attempt_provenance(self) -> None:
        self._register("codex_like", FakeHarnessSession)
        config = AgentConfig(
            skill_models={
                "route-skill": (AgentRouteCandidate("codex_like", "codex-model", effort="medium"),),
            }
        )

        final = self._run(config)

        assert final.reasoning_effort == "medium"

    def test_unsupported_candidate_effort_falls_through_before_dispatch(self) -> None:
        self._register("codex_like", FakeHarnessSession)
        self._register("claude_like", FakeHarnessSession)
        config = AgentConfig(
            skill_models={
                "route-skill": (
                    AgentRouteCandidate("codex_like", "codex-model", effort="max"),
                    AgentRouteCandidate("claude_like", "claude-model", effort="high"),
                ),
            }
        )

        with patch.dict(
            "teatree.agents.model_tiering.HARNESS_EFFORT_SCALE",
            {"codex_like": frozenset({"low", "medium", "high", "xhigh"})},
        ):
            final = self._run(config)

        assert self.built == ["claude_like"]
        assert final.reasoning_effort == "high"
        assert final.route_candidate_index == 1
        assert "effort 'max' is unsupported" in final.fallback_reason

    def test_protocol_failure_does_not_switch_routes(self) -> None:
        self._register("codex_like", _ProtocolFailureSession)
        self._register("claude_like", FakeHarnessSession)

        with pytest.raises(RuntimeError, match="response schema"):
            self._run(self._config("codex_like", "claude_like"))

        assert self.built == ["codex_like"]

    def test_routed_claude_missing_cli_switches_to_the_next_candidate(self) -> None:
        self._register("claude_like", FakeHarnessSession, spawns_cli_child=True)
        self._register("codex_like", FakeHarnessSession)

        with patch.object(runner_mod.shutil, "which", return_value=None):
            final = self._run(self._config("claude_like", "codex_like"))

        assert self.built == ["claude_like", "codex_like"]
        assert final.selected_harness == "codex_like"
        assert final.fallback_reason == "claude is not installed"

    def test_routed_claude_credential_gap_switches_to_the_next_candidate(self) -> None:
        self._register("claude_like", FakeHarnessSession, spawns_cli_child=True)
        self._register("codex_like", FakeHarnessSession)

        with (
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(
                runner_mod, "_provider_child_env", side_effect=runner_mod.CredentialError("credential missing")
            ),
        ):
            final = self._run(self._config("claude_like", "codex_like"))

        assert self.built == ["claude_like", "codex_like"]
        assert final.selected_harness == "codex_like"
        assert final.fallback_reason == "credential missing"

    def test_retryable_failure_after_side_effects_requeues_without_switching(self) -> None:
        self._register("codex_like", _PostSideEffectFailureSession)
        self._register("claude_like", FakeHarnessSession)
        config = self._config("codex_like", "claude_like")

        attempt = self._run(config)

        attempt.task.refresh_from_db()
        assert self.built == ["codex_like"]
        assert attempt.selected_harness == "codex_like"
        assert attempt.agent_session_id == _CODEX_THREAD_ID
        assert attempt.task.status == Task.Status.PENDING
        assert resume_session_id(attempt.task, harness="codex_like") == _CODEX_THREAD_ID
        with patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config):
            retry = resolve_dispatch_harness(attempt.task, phase="debugging", skills=["route-skill"])
        assert retry.name == "codex_like"

    def test_unknown_first_candidate_falls_through_to_installed_second(self) -> None:
        self._register("claude_like", FakeHarnessSession)
        task = self._task()
        config = self._config("codex_like", "claude_like")

        with patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config):
            dispatch = resolve_dispatch_harness(task, phase="debugging", skills=["route-skill"])

        assert dispatch.name == "claude_like"
        assert dispatch.route_candidate_index == 1
        assert str(dispatch.rejected[0]) == "codex_like: not registered"

    def test_skill_route_ignores_an_unrelated_invalid_global_harness_provider_pair(self) -> None:
        self._register("codex_like", FakeHarnessSession)
        task = self._task()
        config = self._config("codex_like")
        unrelated_global = SimpleNamespace(
            agent_harness="pydantic_ai",
            agent_harness_provider=AgentHarnessProvider.SUBSCRIPTION_OAUTH,
        )

        with (
            patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config),
            patch.object(harness_dispatch_mod, "get_effective_settings", return_value=unrelated_global),
        ):
            dispatch = resolve_dispatch_harness(task, phase="debugging", skills=["route-skill"])

        assert dispatch.name == "codex_like"

    def test_invalid_candidate_provider_rejects_that_candidate_and_tries_the_next(self) -> None:
        self._register("codex_like", FakeHarnessSession)
        self._register("claude_like", FakeHarnessSession)
        task = self._task()
        config = AgentConfig(
            skill_models={
                "route-skill": (
                    AgentRouteCandidate("codex_like", "codex-model", provider="not-a-provider"),
                    AgentRouteCandidate("claude_like", "claude-model"),
                )
            }
        )

        with patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config):
            dispatch = resolve_dispatch_harness(task, phase="debugging", skills=["route-skill"])

        assert dispatch.name == "claude_like"
        assert dispatch.route_candidate_index == 1
        assert "Invalid agent_harness_provider" in dispatch.rejected[0].reason

    def test_active_managed_usage_window_skips_codex_without_opening_it(self) -> None:
        self._register("codex_like", FakeHarnessSession)
        self._register("claude_like", FakeHarnessSession)
        UsageWindowState.record_limit(
            lane=TaskAttempt.Lane.MANAGED,
            cause="subscription_weekly",
            resets_at=timezone.now() + timedelta(hours=1),
        )

        final = self._run(self._config("codex_like", "claude_like"))

        assert self.built == ["claude_like"]
        assert final.selected_harness == "claude_like"
        assert final.route_candidate_index == 1
        assert "subscription_weekly window" in final.fallback_reason

    def test_ambiguous_companion_routes_are_recorded_as_one_refusal(self) -> None:
        config = AgentConfig(
            skill_models={
                "code": (AgentRouteCandidate("codex_like", "codex"),),
                "review": (AgentRouteCandidate("claude_like", "opus"),),
            }
        )

        attempt = self._run(config, skills=["code", "review"])

        assert attempt.exit_code == 1
        assert "multiple loaded skills own an ordered agent route: code, review" in attempt.error
        assert TaskAttempt.objects.filter(task=attempt.task).count() == 1
