"""Inert registered harnesses and a route-config patch for tests of ordered skill routes."""

import contextlib
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import teatree.agents.harness_dispatch as harness_dispatch_mod
from teatree.agents import codex_app_server_options, harness_registry
from teatree.agents.harness_registry import HarnessBuildContext, HarnessCapabilities, HarnessSpec, register_harness
from teatree.agents.skill_routing import clear_route_availability_cache
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream

MANAGED = "codex_like"
CLAUDE_LIKE = "claude_like"
CLAUDE_SDK = "claude_sdk"
CODEX_CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "codex_app_server" / "0.155.1-models-cache.json"


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


def failing_session(error: Exception) -> type[FakeHarnessSession]:
    class FailingSession(FakeHarnessSession):
        async def query(self, prompt: str) -> None:
            del prompt
            raise error

    return FailingSession


def scripted_session(messages: list[object]) -> type[FakeHarnessSession]:
    class ScriptedSession(FakeHarnessSession):
        def __init__(self, _ignored: list[object], *, delay: float = 0.0) -> None:
            super().__init__(messages, delay=delay)

    return ScriptedSession


def register_stub_harnesses(
    test: TestCase, *names: str, sessions: dict[str, type[FakeHarnessSession]] | None = None
) -> None:
    """Register a stub harness per name for the test's lifetime; ``codex_like`` rides the managed lane.

    *sessions* swaps the session a named harness opens (default: one that finishes cleanly).
    """
    clear_route_availability_cache()
    test.addCleanup(clear_route_availability_cache)
    for name in names:
        capabilities = HarnessCapabilities(managed_lane=name == MANAGED)
        session = (sessions or {}).get(name, FakeHarnessSession)

        def build(
            _context: HarnessBuildContext,
            caps: HarnessCapabilities = capabilities,
            session: type[FakeHarnessSession] = session,
        ) -> StubHarness:
            return StubHarness(caps, session)

        register_harness(HarnessSpec(name=name, factory=build, capabilities=capabilities, allows_provider=False))
        test.addCleanup(harness_registry._REGISTRY.pop, name, None)


def write_codex_login(home: Path) -> Path:
    """A stand-in ``auth.json`` in a private Codex home, so the Codex candidate's login probe passes."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text("{}")
    return home


def route_config(skill: str, *harnesses: str) -> AgentConfig:
    return AgentConfig(
        skill_models={skill: tuple(AgentRouteCandidate(name, f"{name}-model") for name in harnesses)},
    )


def routed_by(config: AgentConfig) -> AbstractContextManager[object]:
    return patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config)


def make_codex_available(test: TestCase, home: Path) -> None:
    """Let the real Codex candidate pass its host probes for the test's lifetime, with *home* as its private home."""
    for patcher in (
        patch.multiple(
            codex_app_server_options, running_in_container=lambda: False, container_is_the_sandbox=lambda: False
        ),
        patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=False),
        patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
        patch.dict("os.environ", {"T3_CODEX_HOME": str(home)}),
    ):
        test.enterContext(patcher)
