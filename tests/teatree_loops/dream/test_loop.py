"""The ``dream`` mini-loop is discoverable but off the live work loop (#1933).

The dreaming consolidation pass is heavier than a scanner tick and must not
run on — or re-arm — the live 12-minute loop (issue #1933 § 3). It is its own
low-frequency cron (``t3 dream tick``) that reuses the MiniLoop cadence /
config / in-flight-lock primitives. The structural contract: the ``dream``
loop is registered (so its cadence is configured under ``[loops.dream]`` and
the statusline can show its countdown) yet excluded from both the live-tick
fan-out (``build_registry_jobs``) and the orchestrator's normal dispatch.
"""

import datetime as dt
import inspect
from dataclasses import replace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.loop_lease_manager import LoopLeaseManager
from teatree.core.models import MiniLoopMarker
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.dream.loop import (
    DREAM_LEASE_SECONDS,
    DREAM_LOOP_NAME,
    DREAM_PASS_BUDGET_SECONDS,
    MINI_LOOP,
    cross_link_enabled,
    decay_enabled,
    propose_evals_enabled,
    reindex_enabled,
)
from teatree.loops.fanout import build_registry_jobs
from teatree.loops.orchestrator import Orchestrator
from teatree.loops.orchestrator import TickRequest as OrchestratorTickRequest
from teatree.loops.registry import iter_loops

NOW = dt.datetime(2026, 6, 11, 4, tzinfo=dt.UTC)


def _backends() -> list[OverlayBackends]:
    return [
        OverlayBackends(
            name="teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
        ),
    ]


def _context() -> dict[str, object]:
    return {
        "backends": _backends(),
        "host": None,
        "messaging": None,
        "notion_client": None,
        "ready_labels": (),
    }


class DreamMiniLoopShapeTestCase(TestCase):
    def test_loop_name_is_canonical_dream(self) -> None:
        assert MINI_LOOP.name == DREAM_LOOP_NAME == "dream"

    def test_loop_is_off_live_tick(self) -> None:
        assert MINI_LOOP.off_live_tick is True

    def test_default_cadence_is_low_frequency(self) -> None:
        # Nightly-ish: at least a day between passes (the cron drives it).
        assert MINI_LOOP.default_cadence_seconds >= 24 * 3600

    def test_build_jobs_emits_no_scanner_jobs(self) -> None:
        # The engine is invoked by the dream cron, not via the scanner-job
        # dispatch pipeline — so the MiniLoop contributes no _ScannerJob.
        assert MINI_LOOP.build_jobs(**_context()) == []


class DreamLoopRegistrationTestCase(TestCase):
    def test_dream_is_discoverable_in_registry(self) -> None:
        names = {loop.name for loop in iter_loops()}
        assert "dream" in names

    def test_dream_excluded_from_live_tick_fanout(self) -> None:
        # An off-live-tick loop must NOT be marked fired by the live tick — its
        # cadence ledger is owned by its own cron. The anti-vacuous contrast:
        # the SAME loop without the skip flag IS fired by the live fan-out.
        MiniLoopMarker.objects.all().delete()
        build_registry_jobs(_context(), config=LoopsConfig(), now=NOW)
        assert not MiniLoopMarker.objects.filter(name="dream").exists()

        MiniLoopMarker.objects.all().delete()
        live_dream = replace(MINI_LOOP, off_live_tick=False)
        with patch("teatree.loops.fanout.iter_loops", return_value=[live_dream]):
            build_registry_jobs(_context(), config=LoopsConfig(), now=NOW)
        assert MiniLoopMarker.objects.filter(name="dream").exists()

    def test_dream_excluded_from_orchestrator_dispatch(self) -> None:
        MiniLoopMarker.objects.all().delete()
        captured: list[object] = []

        def _dispatch(jobs: list[object]) -> list[object]:
            captured.extend(jobs)
            return list(jobs)

        outcome = Orchestrator(
            config=LoopsConfig(),
            registry_fn=iter_loops,
            clock=lambda: NOW,
            dispatch_fn=_dispatch,
        ).tick(OrchestratorTickRequest(backends=_backends()))
        # The off-live-tick loop is skipped before build/dispatch, so its
        # marker is never bumped by the orchestrator and it is not dispatched.
        assert not MiniLoopMarker.objects.filter(name="dream").exists()
        assert "dream" not in outcome.dispatched_loops
        assert outcome.skipped_loops.get("dream") == "off_live_tick"


class OffLiveTickFieldTestCase(TestCase):
    def test_default_off_live_tick_is_false(self) -> None:
        loop = MiniLoop(name="x", default_cadence_seconds=60, build_jobs=lambda **_: [])
        assert loop.off_live_tick is False


class ProposeEvalsKillSwitchTestCase(TestCase):
    """The nightly eval-derivation seam is LIVE by default, flippable via env/toml (#2346)."""

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.toml = __import__("pathlib").Path(self.tmp) / "t3.toml"

    def test_default_is_on_with_no_env_no_toml(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            assert propose_evals_enabled(config_path=self.toml) is True

    def test_falsy_env_disables(self) -> None:
        for value in ("0", "false", "no", "off", "FALSE"):
            with patch.dict("os.environ", {"T3_DREAM_PROPOSE_EVALS": value}):
                assert propose_evals_enabled(config_path=self.toml) is False, value

    def test_truthy_env_enables(self) -> None:
        with patch.dict("os.environ", {"T3_DREAM_PROPOSE_EVALS": "1"}):
            assert propose_evals_enabled(config_path=self.toml) is True

    def test_toml_false_disables_when_env_absent(self) -> None:
        self.toml.write_text("[loops.dream]\npropose_evals = false\n", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=False):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            assert propose_evals_enabled(config_path=self.toml) is False

    def test_env_falsy_wins_over_toml_true(self) -> None:
        self.toml.write_text("[loops.dream]\npropose_evals = true\n", encoding="utf-8")
        with patch.dict("os.environ", {"T3_DREAM_PROPOSE_EVALS": "0"}):
            assert propose_evals_enabled(config_path=self.toml) is False

    def test_corrupt_toml_falls_back_to_default_on_never_raises(self) -> None:
        # A malformed toml must not take down the nightly cron — default ON.
        self.toml.write_text("[loops.dream]\npropose_evals = = broken\n", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=False):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            assert propose_evals_enabled(config_path=self.toml) is True


class MemoryPhaseKillSwitchTestCase(TestCase):
    """Phases 4-6 are LIVE by default, each flippable via its own env/toml (#1933 §6)."""

    _PHASES = (
        ("cross_link", "T3_DREAM_CROSS_LINK", cross_link_enabled),
        ("reindex", "T3_DREAM_REINDEX", reindex_enabled),
        ("decay", "T3_DREAM_DECAY", decay_enabled),
    )

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.toml = __import__("pathlib").Path(self.tmp) / "t3.toml"

    def _clear_env(self) -> None:
        for _key, env, _fn in self._PHASES:
            __import__("os").environ.pop(env, None)

    def test_each_phase_defaults_on(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            self._clear_env()
            for _key, _env, fn in self._PHASES:
                assert fn(config_path=self.toml) is True, fn.__name__

    def test_each_phase_disabled_by_falsy_env(self) -> None:
        for _key, env, fn in self._PHASES:
            with patch.dict("os.environ", {env: "false"}):
                assert fn(config_path=self.toml) is False, env

    def test_each_phase_disabled_by_toml(self) -> None:
        for key, env, fn in self._PHASES:
            self.toml.write_text(f"[loops.dream]\n{key} = false\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=False):
                __import__("os").environ.pop(env, None)
                assert fn(config_path=self.toml) is False, key

    def test_env_truthy_wins_over_toml_false(self) -> None:
        for key, env, fn in self._PHASES:
            self.toml.write_text(f"[loops.dream]\n{key} = false\n", encoding="utf-8")
            with patch.dict("os.environ", {env: "1"}):
                assert fn(config_path=self.toml) is True, key


class DreamLeaseSizingTestCase(TestCase):
    def test_lease_outlives_the_pass_budget(self) -> None:
        # A default 120s lease would expire mid-pass and let a concurrent pass
        # win the CAS. The lease must outlive the longest pass so "no two
        # overlapping passes" holds.
        assert DREAM_LEASE_SECONDS > DREAM_PASS_BUDGET_SECONDS

    def test_lease_exceeds_the_acquire_default(self) -> None:
        default_ttl = inspect.signature(LoopLeaseManager.acquire).parameters["lease_seconds"].default
        assert default_ttl < DREAM_LEASE_SECONDS
