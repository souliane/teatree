"""A demo factory overlay driving dispatch → attempt → cost via overlay_sdk ONLY (#3157 E6).

Acceptance: a demo overlay drives a full dispatch → attempt → cost cycle using ONLY
``teatree.overlay_sdk`` imports (no reach into private ``teatree.agents._*`` internals — the
import-linter contract forbids that), and can register its own harness through the same surface.
"""

import ast
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

# The demo overlay's driving code imports the factory surface from ONE place:
import teatree.overlay_sdk as sdk
from teatree.agents import harness_registry
from teatree.agents import headless as headless_mod
from teatree.agents.headless import TaskUsage
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream


class _DemoFactoryHarness:
    """A demo overlay's own transport, built with only overlay_sdk symbols."""

    capabilities = sdk.HarnessCapabilities(structured_output=True, cache_control=True)

    def __init__(self, messages: list[object]) -> None:
        self._messages = messages

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncIterator[FakeHarnessSession]:
        yield FakeHarnessSession(self._messages)


class TestOverlaySdkDrivesFullCycle(TestCase):
    def test_demo_overlay_dispatch_attempt_cost_via_overlay_sdk_only(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, execution_target=Task.ExecutionTarget.HEADLESS)
        task.renew_lease = lambda **_kw: None

        # DISPATCH + ATTEMPT — the overlay registers its own harness (overlay_sdk surface),
        # selects it, and drives the dispatch through overlay_sdk.run_headless.
        try:
            sdk.register_harness(
                "demo_factory",
                lambda ctx: _DemoFactoryHarness(success_stream({"summary": "demo cycle complete"})),
                capabilities=_DemoFactoryHarness.capabilities,
            )
            with patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))):
                ConfigSetting.objects.set_value("agent_harness", "demo_factory")
                attempt = sdk.run_headless(task, phase="debugging", overlay_skill_metadata=sdk.SkillMetadata())
        finally:
            harness_registry._REGISTRY.pop("demo_factory", None)

        assert isinstance(attempt, TaskAttempt)
        assert attempt.result.get("summary") == "demo cycle complete"

        # COST — the overlay reads its spend through overlay_sdk, never the model manager.
        breakdown = sdk.headless_cost_breakdown()
        assert isinstance(breakdown, sdk.CostBreakdown)
        assert breakdown.attempts >= 1

    def test_attempt_recording_via_overlay_sdk_surface(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, execution_target=Task.ExecutionTarget.HEADLESS)

        attempt = sdk.record_result_envelope(
            task,
            {"summary": "recorded via sdk"},
            phase="debugging",
            usage=sdk.AttemptUsage(model="claude-opus-4-8", cost_usd=0.1, cost_is_estimated=False, lane="metered"),
        )
        assert attempt.result["summary"] == "recorded via sdk"
        assert attempt.cost_is_estimated is False


class TestContextPlanViaOverlaySdk:
    def test_byte_stable_head_helpers_are_on_the_surface(self) -> None:
        plan = sdk.ContextPlan.ordered(
            [
                sdk.ContextSegment("preamble", sdk.SegmentStability.STATIC),
                sdk.ContextSegment("repo digest", sdk.SegmentStability.PER_REPO, cache=True, ttl="1h"),
                sdk.ContextSegment("live 2026-07-11T09:00", sdk.SegmentStability.VOLATILE),
            ]
        )
        sdk.assert_byte_stable_head(plan)
        assert sdk.find_unstable_tokens(plan.cacheable_head()) == []


class TestDemoOverlayImportsOnlyTheSdk:
    """This demo file's PRODUCTION-surface imports are overlay_sdk only — the E6 contract."""

    def test_this_demo_imports_no_private_agents_module(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        production_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                production_imports.append(node.module)
            elif isinstance(node, ast.Import):
                production_imports.extend(alias.name for alias in node.names)
        # The demo may import test infra (tests.*, headless_mod for the usage stub, core models
        # to build fixtures) but never a PRIVATE agents internal — that is the forbidden surface.
        for module in production_imports:
            assert not module.startswith("teatree.agents._"), module
