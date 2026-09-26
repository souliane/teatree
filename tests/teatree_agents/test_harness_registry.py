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
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.model_tiering as model_tiering_mod
import teatree.agents.runner as runner_mod
import teatree.config as config_mod
from teatree.agents import harness_registry
from teatree.agents.harness import CLAUDE_SDK_CAPABILITIES, ClaudeSdkHarness, PydanticAiHarness, resolve_harness
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.harness_registry import (
    HARNESS_ENTRY_POINT_GROUP,
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessRejection,
    HarnessSpec,
    InvalidHarnessProviderError,
    NoAvailableHarnessError,
    UnknownHarnessError,
    assert_provider_valid_for_harness,
    register_harness,
    registered_harness_names,
    resolve_harness_spec,
    select_harness,
    valid_providers_for,
)
from teatree.agents.pydantic_ai_config import PYDANTIC_AI_ROUTER_CAPABILITIES
from teatree.agents.runner import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat, run_agent
from teatree.config import AgentHarness, AgentHarnessProvider, TeaTreeConfig
from teatree.config.agent_spawn import AgentConfig
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Worktree
from teatree.core.overlay import OverlayBase, OverlayConfig, ProvisionStep
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
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
        assert {"claude_sdk", "codex_app_server", "pydantic_ai"} <= registered_harness_names()

    def test_codex_app_server_resolves_as_a_foss_builtin(self) -> None:
        assert resolve_harness_spec("codex_app_server").name == "codex_app_server"

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
        register_harness(HarnessSpec(name="no_providers_declared", factory=lambda ctx: FakeThirdHarness([])))
        try:
            assert_provider_valid_for_harness("no_providers_declared", "openai_compatible")
        finally:
            harness_registry._REGISTRY.pop("no_providers_declared", None)

    def test_managed_chatgpt_harness_refuses_an_api_provider_pin(self) -> None:
        register_harness(
            HarnessSpec(name="managed_chatgpt", factory=lambda ctx: FakeThirdHarness([]), allows_provider=False)
        )
        try:
            with pytest.raises(InvalidHarnessProviderError, match="must be unset"):
                assert_provider_valid_for_harness("managed_chatgpt", "openai_compatible")
        finally:
            harness_registry._REGISTRY.pop("managed_chatgpt", None)

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
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        task.renew_lease = lambda **_kw: None  # threaded ORM read is a TestCase artifact
        with (
            _register_third_harness_via_entry_point(self._monkeypatch),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))),
        ):
            ConfigSetting.objects.set_value("agent_harness", "fake_third")
            # "debugging" has no phase-evidence gate, so a clean summary completes — proving
            # the whole dispatch → attempt cycle ran through the entry-point harness.
            attempt = run_agent(task, phase="debugging", overlay_skill_metadata=SkillMetadata())

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
        runner_mod._resolve_backend_or_failure,
        runner_mod._resolve_dispatch_lane,
        runner_mod._resolve_child_env_or_failure,
        runner_mod._restore_unconsumed_resume_thread,
        runner_mod._admission_park_or_child_env,
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
        assert runner_mod._resolve_dispatch_lane(PydanticAiHarness(), None) == TaskAttempt.Lane.METERED
        # A non-metered harness with no provider pin stays unattributed.
        assert runner_mod._resolve_dispatch_lane(ClaudeSdkHarness(), None) == ""

    def test_an_overlay_backend_declaring_the_metered_flag_routes_to_the_metered_lane(self) -> None:
        # An overlay-registered backend that sets capabilities.metered_lane drives the same
        # dispatch decision with ZERO isinstance — proving the seam works for a third harness.
        metered_third = FakeThirdHarness([])
        metered_third.capabilities = HarnessCapabilities(metered_lane=True)
        assert runner_mod._resolve_dispatch_lane(metered_third, None) == TaskAttempt.Lane.METERED


def test_programmatic_register_harness_is_resolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda *a, group=None, **k: importlib.metadata.EntryPoints()
    )
    try:
        register_harness(
            HarnessSpec(
                name="prog_harness",
                factory=lambda ctx: FakeThirdHarness([assistant_text("hi"), result_message()]),
                capabilities=HarnessCapabilities(structured_output=True),
            )
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
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        task.renew_lease = lambda **_kw: None
        options = _build_options(task, "ctx", phase="coding", skills=[])
        harness = FakeThirdHarness([assistant_text("hi"), result_message(session_id="s1")])
        with patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))):
            outcome = asyncio.run(_drive_with_heartbeat(task, "p", options, harness, watchdog=_watchdog()))
        assert outcome.result_message is not None


class _CandidateOverlay(OverlayBase):
    def __init__(self, candidates: dict[str, list[str]]) -> None:
        super().__init__()
        self.config = OverlayConfig(factory_phase_harness_candidates=candidates)

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        del worktree
        return []


class _SessionDyingAfterOpen(FakeHarnessSession):
    async def query(self, prompt: str) -> None:
        msg = f"candidate died after opening on {prompt[:10]!r}"
        raise RuntimeError(msg)


@dataclass
class _CandidateLedger:
    built: list[str] = field(default_factory=list)
    probed: list[str] = field(default_factory=list)


class _NamedHarness:
    capabilities = HarnessCapabilities()

    def __init__(self, name: str, session: type[FakeHarnessSession]) -> None:
        self.name = name
        self._session = session

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncIterator[FakeHarnessSession]:
        del options
        yield self._session(success_stream({"summary": f"{self.name} done"}))


def _task_on(overlay: str) -> Task:
    ticket = planned_ticket(overlay=overlay)
    return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))


class _CandidateSeam(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("T3_AGENT_HARNESS", "T3_AGENT_HARNESS_PROVIDER", "T3_OVERLAY_NAME"):
            monkeypatch.delenv(name, raising=False)

    def setUp(self) -> None:
        self.ledger = _CandidateLedger()

    def register(
        self,
        name: str,
        *,
        unavailable: str | None = None,
        flips: bool = False,
        valid_providers: frozenset[str] = frozenset(),
        session: type[FakeHarnessSession] = FakeHarnessSession,
    ) -> None:
        ledger = self.ledger

        def build(context: HarnessBuildContext) -> _NamedHarness:
            del context
            ledger.built.append(name)
            return _NamedHarness(name, session)

        def probe(context: HarnessBuildContext) -> str | None:
            del context
            probed_before = name in ledger.probed
            ledger.probed.append(name)
            if flips and probed_before:
                return "went away after the first probe"
            return unavailable

        register_harness(
            HarnessSpec(
                name=name,
                factory=build,
                unavailable_reason=probe,
                valid_providers=valid_providers,
            )
        )
        self.addCleanup(harness_registry._REGISTRY.pop, name, None)

    @staticmethod
    def installed(candidates_by_overlay: dict[str, dict[str, list[str]]]) -> contextlib.AbstractContextManager[object]:
        overlays = {name: _CandidateOverlay(candidates) for name, candidates in candidates_by_overlay.items()}
        return patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays)

    def dispatch(self, overlay: str) -> TaskAttempt:
        task = _task_on(overlay)
        with (
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))),
        ):
            return run_agent(task, phase="debugging", overlay_skill_metadata=SkillMetadata())


class TestOverlayScopedHarnessCandidates(_CandidateSeam):
    def test_one_overlays_candidate_order_never_reaches_another_overlay(self) -> None:
        self.register("cand_alpha")
        with self.installed(
            {
                "overlay-a": {"coding": ["cand_alpha", "claude_sdk"]},
                "overlay-b": {"coding": ["claude_sdk", "cand_alpha"]},
                "overlay-c": {},
            }
        ):
            on_a = resolve_harness(_task_on("overlay-a"), phase="coding")
            on_b = resolve_harness(_task_on("overlay-b"), phase="coding")
            on_c = resolve_harness(_task_on("overlay-c"), phase="coding")

        assert isinstance(on_a, _NamedHarness)
        assert on_a.name == "cand_alpha"
        assert isinstance(on_b, ClaudeSdkHarness)
        assert isinstance(on_c, ClaudeSdkHarness)

    def test_the_layer_two_pin_follows_the_selected_candidate(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        with self.installed({"overlay-a": {"coding": ["pydantic_ai"]}, "overlay-b": {}}):
            assert resolve_dispatch_harness(_task_on("overlay-a"), phase="coding").provider is None
            assert (
                resolve_dispatch_harness(_task_on("overlay-b"), phase="coding").provider
                is AgentHarnessProvider.SUBSCRIPTION_OAUTH
            )


class TestCandidateFallbackThroughDispatch(_CandidateSeam):
    def test_an_unavailable_first_candidate_dispatches_the_second(self) -> None:
        self.register("cand_down", unavailable="codex binary not on PATH")
        self.register("cand_alpha")
        with self.installed({"overlay-a": {"debugging": ["cand_down", "cand_alpha"]}}):
            attempt = self.dispatch("overlay-a")

        assert attempt.exit_code == 0
        assert attempt.result.get("summary") == "cand_alpha done"
        assert self.ledger.built == ["cand_alpha"]

    def test_a_failure_after_the_harness_opened_never_runs_the_next_candidate(self) -> None:
        self.register("cand_crash", session=_SessionDyingAfterOpen)
        self.register("cand_alpha")
        with (
            self.installed({"overlay-a": {"debugging": ["cand_crash", "cand_alpha"]}}),
            pytest.raises(RuntimeError, match="died after opening"),
        ):
            self.dispatch("overlay-a")

        assert self.ledger.built == ["cand_crash"]

    def test_every_candidate_unavailable_records_exactly_one_refusal(self) -> None:
        self.register("cand_down", unavailable="codex binary not on PATH")
        self.register("cand_gone", unavailable="no credential")
        with self.installed({"overlay-a": {"debugging": ["cand_down", "cand_gone"]}}):
            attempt = self.dispatch("overlay-a")

        assert TaskAttempt.objects.filter(task=attempt.task).count() == 1
        assert "cand_down: codex binary not on PATH" in attempt.error
        assert "cand_gone: no credential" in attempt.error
        assert self.ledger.built == []


class TestEmptyCandidatesKeepTodaysResolution(_CandidateSeam):
    def test_an_empty_or_absent_list_never_consults_availability(self) -> None:
        self.register("cand_down", unavailable="refuses whenever it is probed")
        ConfigSetting.objects.set_value("agent_harness", "cand_down")
        with self.installed({"overlay-a": {}, "overlay-b": {"reviewing": ["claude_sdk"]}}):
            for overlay in ("overlay-a", "overlay-b", "", "not-installed"):
                with self.subTest(overlay=overlay):
                    harness = resolve_harness(_task_on(overlay), phase="coding")
                    assert isinstance(harness, _NamedHarness)
                    assert harness.name == "cand_down"

        assert self.ledger.probed == []

    def test_the_same_harness_listed_as_a_candidate_is_probed_and_refused(self) -> None:
        self.register("cand_down", unavailable="refuses whenever it is probed")
        with (
            self.installed({"overlay-a": {"coding": ["cand_down"]}}),
            pytest.raises(NoAvailableHarnessError, match="refuses whenever it is probed"),
        ):
            resolve_harness(_task_on("overlay-a"), phase="coding")


class TestVerificationResolutionIsUnchangedByCandidates(_CandidateSeam):
    def test_the_shipped_verification_pin_outranks_a_candidate_list(self) -> None:
        self.register("cand_alpha")
        with self.installed({"overlay-a": {"reviewing": ["cand_alpha"]}}):
            assert isinstance(resolve_harness(_task_on("overlay-a"), phase="reviewing"), ClaudeSdkHarness)

    @staticmethod
    def _phase_harness(overrides: dict[str, AgentHarness | None]) -> contextlib.AbstractContextManager[object]:
        return patch.object(
            model_tiering_mod, "resolve_agent_config", return_value=AgentConfig(phase_harness=overrides)
        )

    def test_the_db_override_still_decides_before_the_shipped_pin(self) -> None:
        self.register("cand_alpha")
        candidates = {"overlay-a": {"reviewing": ["cand_alpha"]}}
        with self.installed(candidates), self._phase_harness({"reviewing": AgentHarness.PYDANTIC_AI}):
            assert isinstance(resolve_harness(_task_on("overlay-a"), phase="reviewing"), PydanticAiHarness)

        with self.installed(candidates), self._phase_harness({"reviewing": None}):
            unpinned = resolve_harness(_task_on("overlay-a"), phase="reviewing")
            assert isinstance(unpinned, _NamedHarness)
            assert unpinned.name == "cand_alpha"


class TestSelectHarness:
    def test_rejections_before_the_selection_keep_their_order_and_reason(self) -> None:
        register_harness(
            HarnessSpec(
                name="sel_down",
                factory=lambda ctx: FakeThirdHarness([]),
                unavailable_reason=lambda ctx: "no binary",
            )
        )
        register_harness(HarnessSpec(name="sel_up", factory=lambda ctx: FakeThirdHarness([])))
        try:
            selection = select_harness(["sel_unregistered", "sel_down", "sel_up"], HarnessBuildContext())
        finally:
            harness_registry._REGISTRY.pop("sel_down", None)
            harness_registry._REGISTRY.pop("sel_up", None)

        assert selection.spec.name == "sel_up"
        assert selection.rejected == (
            HarnessRejection("sel_unregistered", "not registered"),
            HarnessRejection("sel_down", "no binary"),
        )

    def test_an_exhausted_list_is_an_unknown_harness_failure(self) -> None:
        with pytest.raises(UnknownHarnessError, match="sel_nothing: not registered"):
            select_harness(["sel_nothing"], HarnessBuildContext())


class TestPathOnlyOverlayCandidates(_CandidateSeam):
    """A path-only overlay carries no importable class, so its table is the only readable source."""

    @staticmethod
    def _path_only(table: dict[str, object]) -> contextlib.ExitStack:
        config = TeaTreeConfig(raw={"overlays": {"t3-path": table}})
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(config_mod, "load_config", return_value=config))
        stack.enter_context(patch("teatree.core.overlay_loader._discover_overlays", return_value={}))
        return stack

    def test_a_path_only_overlay_declaring_nothing_dispatches_unchanged(self) -> None:
        with self._path_only({"path": "~/x/t3-path"}):
            assert isinstance(resolve_harness(_task_on("t3-path"), phase="coding"), ClaudeSdkHarness)

    def test_a_path_only_overlay_declares_its_candidates_in_its_own_table(self) -> None:
        self.register("cand_alpha")
        table = {"path": "~/x/t3-path", "factory_phase_harness_candidates": {"code": ["cand_alpha"]}}
        with self._path_only(table):
            harness = resolve_harness(_task_on("t3-path"), phase="coding")

        assert isinstance(harness, _NamedHarness)
        assert harness.name == "cand_alpha"


class TestOneSelectionPerDispatch(_CandidateSeam):
    """One dispatch selects once.

    A probe answering differently on a second pass must never split the built harness from the
    credential and lane resolved for it.
    """

    def test_a_dispatch_probes_its_candidates_exactly_once(self) -> None:
        # The pin is valid under the FIRST candidate only, so a second selection pass would
        # both re-probe and hand this dispatch a lane the built harness never authenticated on.
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        self.register("cand_flip", flips=True, valid_providers=frozenset({"subscription_oauth"}))
        self.register("cand_alpha", valid_providers=frozenset({"anthropic_api"}))
        with self.installed({"overlay-a": {"debugging": ["cand_flip", "cand_alpha"]}}):
            attempt = self.dispatch("overlay-a")

        assert self.ledger.probed == ["cand_flip"]
        assert self.ledger.built == ["cand_flip"]
        assert attempt.result.get("summary") == "cand_flip done"
        assert attempt.lane == TaskAttempt.Lane.SUBSCRIPTION

    def test_the_provider_comes_from_the_harness_that_selection_built(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        self.register("cand_flip", flips=True, valid_providers=frozenset({"subscription_oauth"}))
        self.register("cand_alpha", valid_providers=frozenset({"anthropic_api"}))
        with self.installed({"overlay-a": {"coding": ["cand_flip", "cand_alpha"]}}):
            dispatch = resolve_dispatch_harness(_task_on("overlay-a"), phase="coding")

        assert dispatch.name == "cand_flip"
        assert isinstance(dispatch.harness, _NamedHarness)
        assert dispatch.harness.name == "cand_flip"
        assert dispatch.provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH
        assert self.ledger.probed == ["cand_flip"]
