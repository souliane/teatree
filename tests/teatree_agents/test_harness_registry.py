"""The OPEN harness registry — overlay-registrable backends + capability flags (#3157 E1).

Acceptance: a test overlay registers a THIRD harness via the ``teatree.harnesses`` entry
point and a dispatch resolves + drives through it with ZERO core edits, and the dispatch code
carries no ``isinstance(harness, …)`` branch — capability/attribute lookups replace it.
"""

import asyncio
import contextlib
import importlib.metadata
import inspect
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.headless as headless_mod
from teatree.agents import harness_registry
from teatree.agents.harness import CLAUDE_SDK_CAPABILITIES, ClaudeSdkHarness, PydanticAiHarness, resolve_harness
from teatree.agents.harness_registry import (
    HARNESS_ENTRY_POINT_GROUP,
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessSpec,
    InvalidHarnessProviderError,
    UnknownHarnessError,
    assert_provider_valid_for_harness,
    register_harness,
    registered_harness_names,
    resolve_harness_spec,
    valid_providers_for,
)
from teatree.agents.headless import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat, run_headless
from teatree.agents.pydantic_ai_config import PYDANTIC_AI_ROUTER_CAPABILITIES
from teatree.config import AgentHarness, AgentHarnessProvider
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from teatree.types import SkillMetadata
from tests.teatree_agents._sdk_fake import FakeHarnessSession, assistant_text, result_message, success_stream


class FakeThirdHarness:
    """A minimal overlay-authored backend — implements only ``open`` + ``capabilities``.

    Proves the acceptance floor: an overlay backend needs to satisfy nothing more than the
    :class:`~teatree.agents.harness.Harness` protocol; the dispatch-lane hints
    (``capabilities.spawns_cli_child`` / ``capabilities.metered_lane``) default off on
    :class:`HarnessCapabilities` (#3157 AH-5), so no CLI child env is resolved and the lane
    is unattributed.
    """

    capabilities = HarnessCapabilities(
        hooks=True, mcp=True, cache_control=True, server_resume=True, structured_output=True
    )

    def __init__(self, messages: list[object]) -> None:
        self._messages = messages

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncIterator[FakeHarnessSession]:
        yield FakeHarnessSession(self._messages)


def _fake_third_spec() -> HarnessSpec:
    return HarnessSpec(
        name="fake_third",
        factory=lambda ctx: FakeThirdHarness(success_stream({"summary": "third-harness done"})),
        capabilities=FakeThirdHarness.capabilities,
        valid_providers=frozenset({"anthropic_api"}),
    )


class _FakeEntryPoint:
    def __init__(self, name: str, spec: HarnessSpec) -> None:
        self.name = name
        self._spec = spec

    def load(self) -> object:
        return lambda: self._spec


@contextlib.contextmanager
def _register_third_harness_via_entry_point(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``teatree.harnesses`` entry point and reset the registry to re-discover it."""
    original = importlib.metadata.entry_points

    def _fake_entry_points(*args: object, group: str | None = None, **kwargs: object) -> object:
        if group == HARNESS_ENTRY_POINT_GROUP:
            return [_FakeEntryPoint("fake_third", _fake_third_spec())]
        return original(*args, group=group, **kwargs) if group is not None else original(*args, **kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake_entry_points)
    harness_registry._reset_registry_for_test()
    try:
        yield
    finally:
        harness_registry._REGISTRY.pop("fake_third", None)
        harness_registry._reset_registry_for_test()


class TestBuiltinRegistrations:
    def test_builtins_are_registered_under_their_enum_values(self) -> None:
        assert {"claude_sdk", "pydantic_ai"} <= registered_harness_names()

    def test_claude_sdk_capabilities_declared(self) -> None:
        spec = resolve_harness_spec("claude_sdk")
        assert spec.capabilities == CLAUDE_SDK_CAPABILITIES
        assert spec.capabilities.server_resume is True
        assert spec.capabilities.cache_control is False

    def test_pydantic_ai_capabilities_and_providers_declared(self) -> None:
        spec = resolve_harness_spec("pydantic_ai")
        assert spec.capabilities == PYDANTIC_AI_ROUTER_CAPABILITIES
        assert spec.valid_providers == frozenset({"openai_compatible", "anthropic_api"})

    def test_registry_valid_providers_agree_with_config_valid_for(self) -> None:
        # The registry's per-backend valid_providers must not drift from the config-layer
        # constraint table (`AgentHarnessProvider.valid_for`) for the built-ins.
        for harness in AgentHarness:
            expected = {p.value for p in AgentHarnessProvider.valid_for(harness)}
            assert resolve_harness_spec(harness.value).valid_providers == frozenset(expected)


class TestProviderConstraintConsumesValidProviders:
    """AH-6: valid_providers is CONSULTED for the harness<->provider constraint, not dead."""

    def test_valid_providers_for_reads_the_registered_set(self) -> None:
        assert valid_providers_for("pydantic_ai") == frozenset({"openai_compatible", "anthropic_api"})

    def test_valid_providers_for_unregistered_name_is_unconstrained(self) -> None:
        assert valid_providers_for("no_such_harness") == frozenset()

    def test_none_provider_always_passes(self) -> None:
        assert_provider_valid_for_harness("pydantic_ai", None)  # no pin → no constraint

    def test_valid_pin_passes(self) -> None:
        assert_provider_valid_for_harness("pydantic_ai", "openai_compatible")

    def test_invalid_pin_raises_naming_the_valid_set(self) -> None:
        with pytest.raises(InvalidHarnessProviderError, match="valid: api_key, subscription_oauth"):
            assert_provider_valid_for_harness("claude_sdk", "openai_compatible")

    def test_under_declared_backend_is_unconstrained(self) -> None:
        # An overlay backend that declared no valid_providers is opt-out (never blocked).
        register_harness("no_providers_declared", lambda ctx: FakeThirdHarness([]))
        try:
            assert_provider_valid_for_harness("no_providers_declared", "openai_compatible")
        finally:
            harness_registry._REGISTRY.pop("no_providers_declared", None)

    def test_unknown_harness_raises(self) -> None:
        with pytest.raises(UnknownHarnessError, match="nope"):
            resolve_harness_spec("nope")


class TestThirdHarnessViaEntryPoint(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        self._monkeypatch = monkeypatch

    def test_entry_point_harness_is_discovered_and_resolved_with_zero_core_edits(self) -> None:
        with _register_third_harness_via_entry_point(self._monkeypatch):
            assert "fake_third" in registered_harness_names()
            ConfigSetting.objects.set_value("agent_harness", "fake_third")
            harness = resolve_harness()
            assert isinstance(harness, FakeThirdHarness)
            assert harness.capabilities.cache_control is True

    def test_dispatch_drives_end_to_end_through_the_entry_point_harness(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, execution_target=Task.ExecutionTarget.HEADLESS)
        task.renew_lease = lambda **_kw: None  # threaded ORM read is a TestCase artifact
        with (
            _register_third_harness_via_entry_point(self._monkeypatch),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))),
        ):
            ConfigSetting.objects.set_value("agent_harness", "fake_third")
            # "debugging" has no phase-evidence gate, so a clean summary completes — proving
            # the whole dispatch → attempt cycle ran through the entry-point harness.
            attempt = run_headless(task, phase="debugging", overlay_skill_metadata=SkillMetadata())

        assert isinstance(attempt, TaskAttempt)
        assert attempt.exit_code == 0
        assert attempt.result.get("summary") == "third-harness done"

    def test_resolve_harness_enforces_the_overlay_backends_own_provider_constraint(self) -> None:
        # AH-6: the fake_third backend declares valid_providers={anthropic_api}. A pinned
        # provider outside that set is rejected at resolve_harness — proving valid_providers
        # is CONSULTED (a live constraint) for an overlay-registered backend the closed-enum
        # valid_for cannot know about.
        with _register_third_harness_via_entry_point(self._monkeypatch):
            ConfigSetting.objects.set_value("agent_harness", "fake_third")
            ConfigSetting.objects.set_value("agent_harness_provider", "openai_compatible")
            with pytest.raises(InvalidHarnessProviderError, match="fake_third"):
                resolve_harness()
            # The declared-valid provider resolves cleanly.
            ConfigSetting.objects.set_value("agent_harness_provider", "anthropic_api")
            assert isinstance(resolve_harness(), FakeThirdHarness)


class TestNoIsInstanceOnHarnessInDispatch:
    """The acceptance guard: no ``isinstance(harness, <HarnessClass>)`` remains in dispatch code."""

    _DISPATCH_CALLABLES = (
        harness_mod.resolve_harness,
        headless_mod._resolve_backend_or_failure,
        headless_mod._resolve_dispatch_lane,
        headless_mod._resolve_child_env_or_failure,
        headless_mod._restore_unconsumed_resume_thread,
        headless_mod._admission_park_or_child_env,
    )

    def test_dispatch_functions_carry_no_isinstance_on_a_harness_class(self) -> None:
        harness_class_names = ("ClaudeSdkHarness", "PydanticAiHarness")
        for func in self._DISPATCH_CALLABLES:
            source = inspect.getsource(func)
            for name in harness_class_names:
                assert f"isinstance(harness, {name}" not in source, f"{func.__name__} isinstance-branches on {name}"
                assert f"isinstance(backend, {name}" not in source, func.__name__


class TestCapabilityDrivenDispatchBehaviour:
    def test_claude_sdk_spawns_cli_child_and_is_not_metered(self) -> None:
        # AH-5: the dispatch-lane hints are typed fields on HarnessCapabilities, not ad-hoc
        # class attributes read by untyped getattr.
        caps = ClaudeSdkHarness().capabilities
        assert caps.spawns_cli_child is True
        assert caps.metered_lane is False

    def test_pydantic_ai_is_metered_and_spawns_no_cli_child(self) -> None:
        caps = PydanticAiHarness().capabilities
        assert caps.metered_lane is True
        assert caps.spawns_cli_child is False

    def test_dispatch_lane_reads_metered_flag_not_isinstance(self) -> None:
        # A metered-flagged harness resolves to the METERED lane regardless of provider —
        # a REAL dispatch decision driven off the typed capabilities.metered_lane flag.
        assert headless_mod._resolve_dispatch_lane(PydanticAiHarness(), None) == TaskAttempt.Lane.METERED
        # A non-metered harness with no provider pin stays unattributed.
        assert headless_mod._resolve_dispatch_lane(ClaudeSdkHarness(), None) == ""

    def test_an_overlay_backend_declaring_the_metered_flag_routes_to_the_metered_lane(self) -> None:
        # An overlay-registered backend that sets capabilities.metered_lane drives the same
        # dispatch decision with ZERO isinstance — proving the seam works for a third harness.
        metered_third = FakeThirdHarness([])
        metered_third.capabilities = HarnessCapabilities(metered_lane=True)
        assert headless_mod._resolve_dispatch_lane(metered_third, None) == TaskAttempt.Lane.METERED


def test_programmatic_register_harness_is_resolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda *a, group=None, **k: importlib.metadata.EntryPoints()
    )
    try:
        register_harness(
            "prog_harness",
            lambda ctx: FakeThirdHarness([assistant_text("hi"), result_message()]),
            capabilities=HarnessCapabilities(structured_output=True),
        )
        spec = resolve_harness_spec("prog_harness")
        assert spec.capabilities.structured_output is True
        assert isinstance(spec.factory(HarnessBuildContext()), FakeThirdHarness)
    finally:
        harness_registry._REGISTRY.pop("prog_harness", None)


class _BrokenEntryPoint:
    """An overlay entry point whose ``load()`` raises — a broken/incompatible backend package."""

    name = "broken_backend"

    def load(self) -> object:
        msg = "overlay harness backend failed to import"
        raise ImportError(msg)


def test_one_broken_entry_point_does_not_kill_resolution_of_the_others(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # AH-7: a single broken overlay entry point raised UNCAUGHT on the first
    # harness resolution, so ALL resolution died. The contract the module documents
    # is a "recorded dispatch failure" that skips only the broken entry point while
    # the working ones still load.
    original = importlib.metadata.entry_points

    def _fake_entry_points(*args: object, group: str | None = None, **kwargs: object) -> object:
        if group == HARNESS_ENTRY_POINT_GROUP:
            return [_BrokenEntryPoint(), _FakeEntryPoint("fake_third", _fake_third_spec())]
        return original(*args, group=group, **kwargs) if group is not None else original(*args, **kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake_entry_points)
    harness_registry._reset_registry_for_test()
    try:
        with caplog.at_level("WARNING", logger="teatree.agents.harness_registry"):
            # Does NOT raise — the broken entry point is skipped, the working one loads.
            names = registered_harness_names()
            spec = resolve_harness_spec("fake_third")
        assert "fake_third" in names
        assert "broken_backend" not in names
        assert spec.name == "fake_third"
        assert any("broken_backend" in rec.getMessage() for rec in caplog.records)
    finally:
        harness_registry._REGISTRY.pop("fake_third", None)
        harness_registry._reset_registry_for_test()


def test_injected_harness_drives_through_seam_with_capabilities() -> None:
    # A pure Harness double with capabilities drives through the seam unchanged.
    harness = FakeThirdHarness([assistant_text("done"), result_message(session_id="s1")])

    async def _drive() -> None:
        async with harness.open(object()) as session:
            await session.query("p")
            messages = [m async for m in session.receive_response()]
        assert messages

    asyncio.run(_drive())


def _watchdog() -> LoopWatchdog:
    return LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)


class TestDriveThroughThirdHarness(TestCase):
    def test_driver_collects_through_a_third_party_harness(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        task.renew_lease = lambda **_kw: None
        options = _build_options(task, "ctx", phase="coding", skills=[])
        harness = FakeThirdHarness([assistant_text("hi"), result_message(session_id="s1")])
        with patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))):
            outcome = asyncio.run(_drive_with_heartbeat(task, "p", options, harness, watchdog=_watchdog()))
        assert outcome.result_message is not None
