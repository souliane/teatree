"""Inert registered harnesses and a route-config patch for tests of ordered skill routes."""

import contextlib
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from unittest.mock import patch

from django.test import TestCase

import teatree.agents.harness_dispatch as harness_dispatch_mod
from teatree.agents import harness_registry
from teatree.agents.harness_registry import HarnessBuildContext, HarnessCapabilities, HarnessSpec, register_harness
from teatree.agents.skill_routing import clear_route_availability_cache
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream

MANAGED = "codex_like"
CLAUDE_LIKE = "claude_like"
CLAUDE_SDK = "claude_sdk"


class CrashingSession(FakeHarnessSession):
    async def query(self, prompt: str) -> None:
        del prompt
        message = "invalid app-server response schema"
        raise RuntimeError(message)


class StubHarness:
    """Opens a session that finishes cleanly, or crashes; the dispatch tests only care which one was built."""

    def __init__(self, capabilities: HarnessCapabilities, session: type[FakeHarnessSession]) -> None:
        self.capabilities = capabilities
        self.session = session

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncIterator[FakeHarnessSession]:
        del options
        yield self.session(success_stream({"summary": "done"}))


def register_stub_harnesses(test: TestCase, *names: str, crashing: tuple[str, ...] = ()) -> None:
    """Register a stub harness per name for the test's lifetime; ``codex_like`` rides the managed lane."""
    clear_route_availability_cache()
    test.addCleanup(clear_route_availability_cache)
    for name in names:
        capabilities = HarnessCapabilities(managed_lane=name == MANAGED)
        session = CrashingSession if name in crashing else FakeHarnessSession

        def build(
            _context: HarnessBuildContext,
            caps: HarnessCapabilities = capabilities,
            session: type[FakeHarnessSession] = session,
        ) -> StubHarness:
            return StubHarness(caps, session)

        register_harness(HarnessSpec(name=name, factory=build, capabilities=capabilities, allows_provider=False))
        test.addCleanup(harness_registry._REGISTRY.pop, name, None)


def route_config(skill: str, *harnesses: str) -> AgentConfig:
    return AgentConfig(
        skill_models={skill: tuple(AgentRouteCandidate(name, f"{name}-model") for name in harnesses)},
    )


def routed_by(config: AgentConfig) -> AbstractContextManager[object]:
    return patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config)
