"""``manage.py dream`` — orchestration for the idle-time dream cron (#1933).

The command owns the cron mechanics around the (stubbed) distillation engine:
the in-flight ``LoopLease`` lock so two passes never overlap, the cadence gate
for ``tick``, the ``DreamRunMarker`` stamping (success vs. attempt), and the
``--dry-run`` no-write path. These are all testable without an LLM because the
engine is a typed seam.
"""

import datetime as dt
from io import StringIO
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConsolidatedMemory, DreamRunMarker, LoopLease, MiniLoopMarker
from teatree.loops.dream.engine import DreamRunResult
from teatree.loops.dream.loop import DREAM_LEASE_NAME, DREAM_LEASE_SECONDS, DREAM_LOOP_NAME

if TYPE_CHECKING:
    from teatree.loops.dream.gates import DreamQaReport


def _ok_result(*, dry_run: bool = False) -> DreamRunResult:
    return DreamRunResult(clusters_recorded=1, members_replayed=3, dry_run=dry_run)


class DreamRunStampsMarkerTestCase(TestCase):
    def test_run_stamps_marker_succeeded(self) -> None:
        before = timezone.now()
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None
        assert marker.last_succeeded_at >= before
        assert marker.last_attempted_at == marker.last_succeeded_at

    def test_run_clears_staleness(self) -> None:
        # A stale engine (never succeeded) is the bootstrap state.
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ):
            call_command("dream", "run", stdout=StringIO())
        assert DreamRunMarker.objects.is_stale(timezone.now()) is False

    def test_failed_run_bumps_attempt_only_keeps_stale(self) -> None:
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            side_effect=RuntimeError("engine boom"),
        ):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_attempted_at is not None
        assert marker.last_succeeded_at is None
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True


class DreamDryRunTestCase(TestCase):
    def test_dry_run_writes_no_marker_and_no_rows(self) -> None:
        called: dict[str, object] = {}

        def _capture(*, overlay: str, since: object, dry_run: bool, eval_proposals: object = None) -> DreamRunResult:
            called["dry_run"] = dry_run
            called["eval_proposals"] = eval_proposals
            return _ok_result(dry_run=dry_run)

        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture):
            call_command("dream", "run", "--dry-run", stdout=StringIO())

        assert called["dry_run"] is True
        assert called["eval_proposals"] is None
        assert not DreamRunMarker.objects.exists()
        assert ConsolidatedMemory.objects.count() == 0

    def test_dry_run_writes_no_marker_when_engine_raises(self) -> None:
        # A dry-run promises "no rows or marker written" — even an attempt
        # marker must not be stamped when the engine raises under --dry-run.
        stdout = StringIO()
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            side_effect=RuntimeError("engine boom"),
        ):
            call_command("dream", "run", "--dry-run", stdout=stdout)
        assert "FAIL" in stdout.getvalue()
        assert not DreamRunMarker.objects.exists()


class DreamProposeEvalsFlagTestCase(TestCase):
    @staticmethod
    def _capture(seen: dict[str, object]):
        def _run(*, overlay: str, since: object, dry_run: bool, eval_proposals: object = None) -> DreamRunResult:
            seen["eval_proposals"] = eval_proposals
            return _ok_result()

        return _run

    def test_propose_evals_off_by_default(self) -> None:
        seen: dict[str, object] = {}
        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)):
            call_command("dream", "run", stdout=StringIO())
        assert seen["eval_proposals"] is None

    def test_propose_evals_flag_enables_the_phase(self) -> None:
        seen: dict[str, object] = {}
        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)):
            call_command("dream", "run", "--propose-evals", stdout=StringIO())
        assert seen["eval_proposals"] is not None

    def test_db_setting_enables_the_phase_for_the_run_path(self) -> None:
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("dream_propose_evals", value=True)
        seen: dict[str, object] = {}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)),
            patch.dict("os.environ", {}, clear=False) as env,
        ):
            env.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "run", stdout=StringIO())
        assert seen["eval_proposals"] is not None

    def test_db_setting_off_keeps_the_run_path_disabled(self) -> None:
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("dream_propose_evals", value=False)
        seen: dict[str, object] = {}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)),
            patch.dict("os.environ", {}, clear=False) as env,
        ):
            env.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "run", stdout=StringIO())
        assert seen["eval_proposals"] is None


class DreamNightlyTickRequestsProposalsTestCase(TestCase):
    """The cadence-driven ``tick`` now requests eval proposals by default (#2346)."""

    @staticmethod
    def _capture(seen: dict[str, object]):
        def _run(*, overlay: str, since: object, dry_run: bool, eval_proposals: object = None) -> DreamRunResult:
            seen["eval_proposals"] = eval_proposals
            return _ok_result()

        return _run

    def test_tick_requests_proposals_by_default(self) -> None:
        seen: dict[str, object] = {}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch.dict("os.environ", {}, clear=False),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=StringIO())
        # The seam is LIVE by default: tick passes a real EvalProposalRequest.
        assert seen["eval_proposals"] is not None

    def test_tick_disabled_by_falsy_env(self) -> None:
        seen: dict[str, object] = {}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", side_effect=self._capture(seen)),
            patch.dict("os.environ", {"T3_DREAM_PROPOSE_EVALS": "0"}),
        ):
            call_command("dream", "tick", stdout=StringIO())
        assert seen["eval_proposals"] is None

    def test_successful_tick_runs_guarded_promotion(self) -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file") as promote_fn,
            patch.dict("os.environ", {}, clear=False),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            promote_fn.return_value = []
            call_command("dream", "tick", stdout=StringIO())
        # A successful pass that requested proposals drives the guarded promotion.
        promote_fn.assert_called_once()

    def test_promotion_failure_is_warned_not_crashed(self) -> None:
        # A promotion error must NOT crash the pass that already stamped success.
        stdout = StringIO()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", side_effect=RuntimeError("promote boom")),
            patch.dict("os.environ", {}, clear=False),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=stdout)
        out = stdout.getvalue()
        assert "WARN eval promotion raised: RuntimeError" in out
        # The pass still stamped success despite the promotion warning.
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None

    def test_promotion_count_in_summary_line(self) -> None:
        from teatree.loops.dream.promote import PromotionOutcome  # noqa: PLC0415

        outcomes = [
            PromotionOutcome(scenario_name="a_under_load", promoted=True, reason="promoted"),
            PromotionOutcome(scenario_name="b_under_load", promoted=False, reason="REJECTED (anti-vacuity)"),
        ]
        stdout = StringIO()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=outcomes),
            patch.dict("os.environ", {}, clear=False),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=stdout)
        out = stdout.getvalue()
        assert "promoted 1 live eval(s)" in out
        assert "withheld 1 unvalidated candidate(s)" in out


class DreamDeriveEvalsWiringTestCase(TestCase):
    """The default-OFF LLM full-scenario derivation only runs when its toggle is on (#2447)."""

    def test_derivation_skipped_when_toggle_off(self) -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.loops.dream.llm_eval_proposer.stage_proposals_file") as stage_fn,
            patch.dict("os.environ", {"T3_DREAM_DERIVE_EVALS": "0"}),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=StringIO())
        stage_fn.assert_not_called()

    def test_derivation_runs_and_reports_when_toggle_on(self) -> None:
        from teatree.loops.dream.llm_eval_proposer import DerivationOutcome  # noqa: PLC0415

        outcomes = [
            DerivationOutcome(scenario_name="a_under_load", derived=True, reason="proven"),
            DerivationOutcome(scenario_name="b_under_load", derived=False, reason="DROPPED (anti-vacuity)"),
        ]
        stdout = StringIO()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.loops.dream.llm_eval_proposer.stage_proposals_file", return_value=outcomes),
            patch.dict("os.environ", {"T3_DREAM_DERIVE_EVALS": "1"}),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=stdout)
        out = stdout.getvalue()
        assert "staged 1 derived eval(s) for review, dropped 1" in out

    def test_derivation_failure_is_warned_not_crashed(self) -> None:
        stdout = StringIO()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch(
                "teatree.loops.dream.llm_eval_proposer.stage_proposals_file",
                side_effect=RuntimeError("derive boom"),
            ),
            patch.dict("os.environ", {"T3_DREAM_DERIVE_EVALS": "1"}),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=stdout)
        out = stdout.getvalue()
        assert "WARN eval derivation raised: RuntimeError" in out
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None


class DreamLiveValidationGateWiringTestCase(TestCase):
    """``--validate-live`` (folded into ``--full``) supplies the metered live validator.

    Promotion now lands a scenario ONLY when it passes a live pass@k. The nightly
    ``tick`` must NOT run the metered validator (so it never auto-lands — correct
    now), while ``t3 dream run --full`` opts in. The wiring is verified by
    capturing the ``live_validator`` kwarg the command threads into
    ``promote_proposals_file`` — no real metered model runs.
    """

    @staticmethod
    def _captured_validator(seen: dict[str, object]):
        def _promote(proposals_path: object, **kwargs: object) -> list:
            gate = kwargs.get("live_gate")
            seen["validator"] = getattr(gate, "validator", "MISSING")
            return []

        return _promote

    def test_tick_does_not_run_the_metered_validator(self) -> None:
        seen: dict[str, object] = {}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", side_effect=self._captured_validator(seen)),
            patch.dict("os.environ", {}, clear=False),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=StringIO())
        # The nightly tick withholds: no metered validator, so nothing auto-lands.
        assert seen["validator"] is None

    def test_run_full_supplies_the_metered_validator(self) -> None:
        seen: dict[str, object] = {}
        sentinel = object()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", side_effect=self._captured_validator(seen)),
            patch("teatree.loops.dream.promote.build_live_validator", return_value=sentinel),
        ):
            call_command("dream", "run", "--full", stdout=StringIO())
        assert seen["validator"] is sentinel

    def test_run_full_lands_a_passing_candidate_and_withholds_a_failing_one(self) -> None:
        # End-to-end through the command into the REAL promote pipeline, with a FAKE
        # validator passing the first candidate and failing the second — no real
        # model call. The passing one lands; the failing one is withheld. The output
        # dirs are redirected at the promote module so nothing touches the repo's evals/.
        import json  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from teatree.loops.dream import promote  # noqa: PLC0415

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        scenarios = tmp / "scenarios"
        fixtures = tmp / "fixtures"
        queue = tmp / "proposals.jsonl"
        passing = {
            "scenario_name": "passing_under_load",
            "drift_rule": "the main agent dispatches the fix to a sub-agent instead of editing in the foreground",
            "seed_citation": "edited session.py in the main agent",
            "lane": "under_load",
            "status": "candidate",
        }
        failing = {**passing, "scenario_name": "failing_under_load"}
        queue.write_text(json.dumps(passing) + "\n" + json.dumps(failing) + "\n", encoding="utf-8")

        def _fake_validator(spec: object, *, trials: int, require: str) -> bool:
            return getattr(spec, "name", "") == "passing_under_load"

        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.build_live_validator", return_value=_fake_validator),
            patch("teatree.loops.dream.promote.SCENARIOS_DIR", scenarios),
            patch("teatree.loops.dream.promote.FIXTURES_DIR", fixtures),
            patch("teatree.loops.dream.eval_proposer._default_proposals_path", return_value=queue),
        ):
            call_command("dream", "run", "--full", stdout=StringIO())

        names = list(promote.loaded_scenario_names(scenarios / "promoted_drift.yaml"))
        assert names == ["passing_under_load"]
        # The failing candidate was withheld — recorded terminal-rejected (a live-FAIL verdict).
        rows = {json.loads(line)["scenario_name"]: json.loads(line) for line in queue.read_text().splitlines()}
        assert rows["passing_under_load"]["status"] == "promoted"
        assert rows["failing_under_load"]["status"] == "rejected"


class DreamMemoryPhasesPipelineTestCase(TestCase):
    """A successful tick runs phases 4-6 over the discovered memory dirs (#1933 §6).

    All tests inject a TMP memory dir via ``discover_memory_dirs`` — the real
    ``~/.claude`` is never touched.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        self.memdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Two related memories so phase 4 has an edge to add and phase 5 has lines.
        topic = "the worktree provision lease pid claim guard owner liveness anchored"
        (self.memdir / "mem_a.md").write_text(f"name: mem_a\n{topic}\n", encoding="utf-8")
        (self.memdir / "mem_b.md").write_text(f"name: mem_b\n{topic} session\n", encoding="utf-8")

    #: All four phase toggles cleared to default-ON unless a test overrides one.
    _PHASE_ENV: ClassVar[dict[str, str]] = {
        "T3_DREAM_PROPOSE_EVALS": "",
        "T3_DREAM_CROSS_LINK": "",
        "T3_DREAM_REINDEX": "",
        "T3_DREAM_DECAY": "",
    }

    def _tick(self, stdout: StringIO, *, env: dict[str, str] | None = None) -> None:
        environ = {**self._PHASE_ENV, **(env or {})}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch.dict("os.environ", environ, clear=False),
        ):
            call_command("dream", "tick", stdout=stdout)

    def test_phases_run_by_default_and_report(self) -> None:
        stdout = StringIO()
        self._tick(stdout)
        out = stdout.getvalue()
        # Phase 4 linked the two related memories; phase 5 regenerated the index.
        assert "cross-linked" in out
        assert "re-indexed" in out
        assert (self.memdir / "MEMORY.md").is_file()
        assert "[[mem_b]]" in (self.memdir / "mem_a.md").read_text(encoding="utf-8")

    def test_phase_disabled_by_kill_switch_does_not_run(self) -> None:
        stdout = StringIO()
        self._tick(stdout, env={"T3_DREAM_CROSS_LINK": "0"})
        # cross-link disabled -> no link added, no cross-link clause.
        assert "[[mem_b]]" not in (self.memdir / "mem_a.md").read_text(encoding="utf-8")
        assert "cross-linked" not in stdout.getvalue()

    def test_reindex_disabled_writes_no_index(self) -> None:
        stdout = StringIO()
        self._tick(stdout, env={"T3_DREAM_REINDEX": "0"})
        assert "re-indexed" not in stdout.getvalue()
        assert not (self.memdir / "MEMORY.md").exists()

    def test_decay_disabled_archives_nothing(self) -> None:
        # An old, unreferenced memory that decay WOULD archive — but decay is off.
        import os  # noqa: PLC0415
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        old = (datetime.now(tz=UTC) - timedelta(days=90)).timestamp()
        stale = self.memdir / "mem_old.md"
        stale.write_text("name: mem_old\nan old unreferenced lesson\n", encoding="utf-8")
        os.utime(stale, (old, old))
        stdout = StringIO()
        self._tick(stdout, env={"T3_DREAM_DECAY": "0"})
        assert "archived" not in stdout.getvalue()
        assert stale.exists()

    def test_one_phase_failing_does_not_crash_the_tick(self) -> None:
        stdout = StringIO()
        with patch("teatree.loops.dream.cross_link.cross_link_memories", side_effect=RuntimeError("phase4 boom")):
            self._tick(stdout)
        out = stdout.getvalue()
        # The pass still stamped success; the phase failure is warned, not fatal,
        # and the LATER phases still ran.
        assert "WARN cross-link raised: RuntimeError" in out
        assert "re-indexed" in out
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None

    def test_no_memory_dirs_is_clean_noop(self) -> None:
        stdout = StringIO()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch.dict("os.environ", {}, clear=False),
        ):
            __import__("os").environ.pop("T3_DREAM_PROPOSE_EVALS", None)
            call_command("dream", "tick", stdout=stdout)
        out = stdout.getvalue()
        assert "cross-linked" not in out
        assert "OK    dream pass" in out


class DreamAcceptanceGateWiringTestCase(TestCase):
    """A failing §4 acceptance gate must NOT stamp the pass succeeded (#2545).

    The gates make the pass anti-vacuous: a lossy / delete-only / no-op
    consolidation FAILS a gate, and the command must keep staleness firing
    rather than launder it into a success. The memory dir is a TMP fixture.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        self.memdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        topic = "the worktree provision lease pid claim guard owner liveness anchored"
        (self.memdir / "mem_a.md").write_text(f"name: mem_a\n{topic}\n", encoding="utf-8")
        (self.memdir / "mem_b.md").write_text(f"name: mem_b\n{topic} session\n", encoding="utf-8")

    def _run(self, stdout: StringIO, *, report: "DreamQaReport") -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch("teatree.loops.dream.gates.run_acceptance_pass", return_value=report),
            patch.dict(
                "os.environ",
                {
                    "T3_DREAM_PROPOSE_EVALS": "",
                    "T3_DREAM_CROSS_LINK": "0",
                    "T3_DREAM_REINDEX": "0",
                    "T3_DREAM_DECAY": "0",
                },
                clear=False,
            ),
        ):
            call_command("dream", "run", stdout=stdout)

    def test_failing_gate_does_not_stamp_succeeded(self) -> None:
        from teatree.loops.dream.gates import DreamQaReport, GateResult  # noqa: PLC0415

        failing = DreamQaReport(gate_results=(GateResult(name="retention", passed=False, detail="lost mem_a"),))
        stdout = StringIO()
        self._run(stdout, report=failing)
        marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
        assert marker is None or marker.last_succeeded_at is None
        out = stdout.getvalue()
        assert "acceptance gate(s) FAILED" in out
        assert "retention" in out

    def test_failing_gate_keeps_staleness_active(self) -> None:
        from teatree.loops.dream.gates import DreamQaReport, GateResult  # noqa: PLC0415

        failing = DreamQaReport(gate_results=(GateResult(name="consolidation", passed=False, detail="no-op pass"),))
        self._run(StringIO(), report=failing)
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True

    def test_passing_gates_stamp_succeeded(self) -> None:
        from teatree.loops.dream.gates import DreamQaReport, GateResult  # noqa: PLC0415

        passing = DreamQaReport(gate_results=(GateResult(name="retention", passed=True, detail="ok"),))
        stdout = StringIO()
        self._run(stdout, report=passing)
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None
        assert "all acceptance gates passed" in stdout.getvalue()

    def test_real_gates_populate_the_dream_qa_probe_corpus(self) -> None:
        # No gate patch — the REAL gates run over the tmp memory dir and POPULATE
        # the formerly-dead DreamQaProbe model (the #2545 core acceptance).
        from teatree.core.models import DreamQaProbe  # noqa: PLC0415

        stdout = StringIO()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch.dict(
                "os.environ",
                {
                    "T3_DREAM_PROPOSE_EVALS": "",
                    "T3_DREAM_CROSS_LINK": "0",
                    "T3_DREAM_REINDEX": "0",
                    "T3_DREAM_DECAY": "0",
                },
                clear=False,
            ),
        ):
            call_command("dream", "run", stdout=stdout)
        # One probe per memory file, recorded against the corpus.
        assert DreamQaProbe.objects.count() == 2
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None


class DreamZeroClusterMaintenanceStampsSucceededTestCase(TestCase):
    """A 0-NEW-cluster pass whose file-side phases did real work stamps success (#2626).

    A live ``run --full`` replayed members, distilled 0 NEW clusters (no new drift
    that night), but cross-linked memory edges and re-indexed MEMORY.md — real
    consolidation maintenance. The §4 consolidation gate must count that as
    consolidation so the ``DreamRunMarker`` is stamped succeeded and the staleness
    alarm clears. The memory dir is a TMP fixture; ``~/.claude`` is never touched.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        self.memdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        topic = "the worktree provision lease pid claim guard owner liveness anchored"
        (self.memdir / "mem_a.md").write_text(f"name: mem_a\n{topic}\n", encoding="utf-8")
        (self.memdir / "mem_b.md").write_text(f"name: mem_b\n{topic} session\n", encoding="utf-8")

    def _zero_cluster_tick(self, stdout: StringIO) -> None:
        # members replayed > 0 (transcript was processed) but 0 NEW clusters distilled.
        zero_cluster = DreamRunResult(clusters_recorded=0, members_replayed=1110, dry_run=False)
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_cluster),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch.dict(
                "os.environ",
                {
                    "T3_DREAM_PROPOSE_EVALS": "",
                    "T3_DREAM_CROSS_LINK": "",
                    "T3_DREAM_REINDEX": "",
                    "T3_DREAM_DECAY": "",
                },
                clear=False,
            ),
        ):
            call_command("dream", "tick", stdout=stdout)

    def test_zero_clusters_with_maintenance_stamps_succeeded(self) -> None:
        stdout = StringIO()
        self._zero_cluster_tick(stdout)
        out = stdout.getvalue()
        # The file-side phases did real work (cross-link + re-index).
        assert "cross-linked" in out
        assert "re-indexed" in out
        # The consolidation gate counted that maintenance: success stamped, staleness cleared.
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None
        assert "all acceptance gates passed" in out
        assert DreamRunMarker.objects.is_stale(timezone.now()) is False


class DreamMemoryPromotionWiringTestCase(TestCase):
    """Pass-2 memory promotion only runs when its default-OFF toggle is on (#2426)."""

    def _tick(self, stdout: StringIO, *, env: dict[str, str]) -> None:
        environ = {"T3_DREAM_PROPOSE_EVALS": "0", "T3_DREAM_CROSS_LINK": "0", "T3_DREAM_REINDEX": "0", **env}
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch.dict("os.environ", environ, clear=False),
        ):
            call_command("dream", "tick", stdout=stdout)

    def test_promotion_skipped_when_toggle_off(self) -> None:
        with patch("teatree.loops.dream.promote_memory.file_core_gap_tickets") as file_fn:
            self._tick(StringIO(), env={"T3_DREAM_MEMORY_PROMOTE": "0"})
        file_fn.assert_not_called()

    def test_promotion_runs_and_reports_when_toggle_on(self) -> None:
        from teatree.loops.dream.promote_memory import TicketOutcome  # noqa: PLC0415

        filed = [TicketOutcome(cluster_key="k1", filed=True, ticket_url="https://example/1")]
        stdout = StringIO()
        with (
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=filed),
            patch("teatree.loops.dream.promote_memory.retire_resolved_memories", return_value=[]),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
        ):
            self._tick(stdout, env={"T3_DREAM_MEMORY_PROMOTE": "1"})
        assert "ticketed 1 core-gap memory(ies), retired 0" in stdout.getvalue()

    def test_promotion_failure_is_warned_not_crashed(self) -> None:
        stdout = StringIO()
        with (
            patch(
                "teatree.loops.dream.promote_memory.file_core_gap_tickets",
                side_effect=RuntimeError("ticket boom"),
            ),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
        ):
            self._tick(stdout, env={"T3_DREAM_MEMORY_PROMOTE": "1"})
        out = stdout.getvalue()
        assert "WARN memory promotion raised: RuntimeError" in out
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None

    def test_no_code_host_is_warned_not_crashed(self) -> None:
        stdout = StringIO()
        with patch(
            "teatree.core.management.commands.dream.Command._teatree_backlog_host",
            return_value=(None, "souliane/teatree"),
        ):
            self._tick(stdout, env={"T3_DREAM_MEMORY_PROMOTE": "1"})
        assert "no teatree code host resolved" in stdout.getvalue()


class DreamFullFlagTestCase(TestCase):
    """``run --full`` forces every phase on for one manual pass (Gap B)."""

    def test_full_requests_eval_proposals(self) -> None:
        seen: dict[str, object] = {}

        def _capture(*, overlay: str, since: object, dry_run: bool, eval_proposals: object = None) -> DreamRunResult:
            seen["eval_proposals"] = eval_proposals
            return _ok_result()

        with (
            patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=[]),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
            patch.dict("os.environ", {"T3_DREAM_MEMORY_PROMOTE": "0", "T3_DREAM_DERIVE_EVALS": "0"}, clear=False),
        ):
            call_command("dream", "run", "--full", stdout=StringIO())
        assert seen["eval_proposals"] is not None

    def test_full_runs_memory_promotion_despite_toggle_off(self) -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=[]) as file_fn,
            patch("teatree.loops.dream.promote_memory.retire_resolved_memories", return_value=[]),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
            patch.dict("os.environ", {"T3_DREAM_MEMORY_PROMOTE": "0"}, clear=False),
        ):
            call_command("dream", "run", "--full", stdout=StringIO())
        file_fn.assert_called_once()

    def test_full_runs_eval_derivation_despite_toggle_off(self) -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=[]),
            patch("teatree.loops.dream.promote_memory.retire_resolved_memories", return_value=[]),
            patch("teatree.loops.dream.llm_eval_proposer.stage_proposals_file", return_value=[]) as stage_fn,
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
            patch.dict("os.environ", {"T3_DREAM_DERIVE_EVALS": "0"}, clear=False),
        ):
            call_command("dream", "run", "--full", stdout=StringIO())
        stage_fn.assert_called_once()

    def test_full_dry_run_previews_but_writes_nothing(self) -> None:
        seen: dict[str, object] = {}

        def _capture(*, overlay: str, since: object, dry_run: bool, eval_proposals: object = None) -> DreamRunResult:
            seen["dry_run"] = dry_run
            seen["eval_proposals"] = eval_proposals
            return _ok_result(dry_run=dry_run)

        with (
            patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=[]),
            patch("teatree.loops.dream.llm_eval_proposer.stage_proposals_file", return_value=[]),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
            patch.dict("os.environ", {"T3_DREAM_MEMORY_PROMOTE": "0", "T3_DREAM_DERIVE_EVALS": "0"}, clear=False),
        ):
            call_command("dream", "run", "--full", "--dry-run", stdout=StringIO())
        # --full composes with --dry-run: the engine previews with proposals requested,
        # but no marker is stamped and no rows are written.
        assert seen["dry_run"] is True
        assert seen["eval_proposals"] is not None
        assert not DreamRunMarker.objects.exists()


class DreamLeaseTtlTestCase(TestCase):
    def test_run_acquires_lease_sized_to_the_pass_budget(self) -> None:
        # The default 120s lease would expire under a wall-clock-capped pass and
        # let a concurrent pass win the CAS. The command must size the lease to
        # the pass budget so "no two overlapping passes" holds for the whole pass.
        captured: dict[str, object] = {}
        real_acquire = LoopLease.objects.acquire

        def _spy(name: str, *, owner: str, lease_seconds: int = 120) -> bool:
            captured["name"] = name
            captured["lease_seconds"] = lease_seconds
            return real_acquire(name, owner=owner, lease_seconds=lease_seconds)

        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch.object(type(LoopLease.objects), "acquire", side_effect=_spy),
        ):
            call_command("dream", "run", stdout=StringIO())

        assert captured["name"] == DREAM_LEASE_NAME
        assert captured["lease_seconds"] == DREAM_LEASE_SECONDS


class DreamInFlightLockTestCase(TestCase):
    def test_overlapping_run_skips_when_lease_held(self) -> None:
        # Simulate a concurrent pass already holding the lease.
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner="other-pid")
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation") as engine:
            call_command("dream", "run", stdout=stdout)
        engine.assert_not_called()
        assert "SKIP" in stdout.getvalue()
        # The loser never stamps a marker.
        assert not DreamRunMarker.objects.exists()

    def test_lease_released_after_run(self) -> None:
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()):
            call_command("dream", "run", stdout=StringIO())
        # A fresh run can re-acquire — the lease was released in finally.
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner="next-pid")

    def test_lease_released_even_when_engine_raises(self) -> None:
        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=RuntimeError("boom")):
            call_command("dream", "run", stdout=StringIO())
        assert LoopLease.objects.acquire(DREAM_LEASE_NAME, owner="after-failure")


class DreamTickCadenceTestCase(TestCase):
    def test_tick_runs_when_cadence_elapsed(self) -> None:
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ) as engine:
            call_command("dream", "tick", stdout=StringIO())
        engine.assert_called_once()
        assert MiniLoopMarker.objects.filter(name=DREAM_LOOP_NAME).exists()

    def test_tick_skips_when_cadence_not_elapsed(self) -> None:
        MiniLoopMarker.objects.mark_fired(DREAM_LOOP_NAME, timezone.now())
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation") as engine:
            call_command("dream", "tick", stdout=stdout)
        engine.assert_not_called()
        assert "SKIP" in stdout.getvalue()

    def test_run_ignores_cadence_gate(self) -> None:
        # `run` is the manual escape hatch — it runs regardless of cadence.
        MiniLoopMarker.objects.mark_fired(DREAM_LOOP_NAME, timezone.now())
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ) as engine:
            call_command("dream", "run", stdout=StringIO())
        engine.assert_called_once()

    def test_tick_failed_engine_does_not_advance_cadence_ledger(self) -> None:
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            side_effect=RuntimeError("engine boom"),
        ):
            call_command("dream", "tick", stdout=StringIO())
        assert not MiniLoopMarker.objects.filter(name=DREAM_LOOP_NAME).exists()


class DreamZeroMembersFailLoudTestCase(TestCase):
    def test_zero_members_does_not_stamp_succeeded(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
        assert marker is None or marker.last_succeeded_at is None

    def test_zero_members_stamps_attempted(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
        assert marker is not None
        assert marker.last_attempted_at is not None

    def test_zero_members_emits_warn(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=stdout)
        assert "WARN" in stdout.getvalue()

    def test_zero_members_keeps_staleness_alarm_active(self) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result):
            call_command("dream", "run", stdout=StringIO())
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True

    def test_nonzero_members_stamps_succeeded(self) -> None:
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()):
            call_command("dream", "run", stdout=StringIO())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None


class DreamZeroMembersStillRunsMemoryPhasesTestCase(TestCase):
    """A 0-transcript pass must still run the file-side phases 4-6 (#2547).

    ``enumerate_members`` finds 0 transcript members on a pass where no recent
    session ran, but the memory ``.md`` files on disk are unchanged — the
    file-side maintenance (cross-link / re-index / decay) operates on
    ``discover_memory_dirs`` and is independent of the transcript extract. A
    0-member pass must still run those phases, while keeping the consolidation
    pass attempted-not-succeeded (staleness stays active — no distillation
    happened). The memory dir is a TMP fixture; the real ``~/.claude`` is never
    touched.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        self.memdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        topic = "the worktree provision lease pid claim guard owner liveness anchored"
        (self.memdir / "mem_a.md").write_text(f"name: mem_a\n{topic}\n", encoding="utf-8")
        (self.memdir / "mem_b.md").write_text(f"name: mem_b\n{topic} session\n", encoding="utf-8")

    def _zero_member_tick(self, stdout: StringIO) -> None:
        zero_result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=False)
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_result),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch.dict(
                "os.environ",
                {
                    "T3_DREAM_PROPOSE_EVALS": "",
                    "T3_DREAM_CROSS_LINK": "",
                    "T3_DREAM_REINDEX": "",
                    "T3_DREAM_DECAY": "",
                },
                clear=False,
            ),
        ):
            call_command("dream", "tick", stdout=stdout)

    def test_zero_members_still_cross_links_and_reindexes(self) -> None:
        stdout = StringIO()
        self._zero_member_tick(stdout)
        out = stdout.getvalue()
        # Phases 4-5 ran over the on-disk memory set despite 0 transcript members.
        assert "cross-linked" in out
        assert "re-indexed" in out
        assert (self.memdir / "MEMORY.md").is_file()
        assert "[[mem_b]]" in (self.memdir / "mem_a.md").read_text(encoding="utf-8")

    def test_zero_members_keeps_pass_attempted_not_succeeded(self) -> None:
        # The consolidation pass found nothing to distil — staleness stays active.
        self._zero_member_tick(StringIO())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_attempted_at is not None
        assert marker.last_succeeded_at is None
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True


class DreamEmptyBatchSummaryTestCase(TestCase):
    """A batch that distils 0 clusters from non-empty input is surfaced in the summary (#1933)."""

    def test_summary_surfaces_empty_batch_count(self) -> None:
        result = DreamRunResult(clusters_recorded=0, members_replayed=4153, dry_run=False, empty_batches=2)
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=result):
            call_command("dream", "run", stdout=stdout)
        out = stdout.getvalue()
        assert "2 batch(es) returned 0 clusters from non-empty input" in out

    def test_no_empty_batches_omits_the_warning(self) -> None:
        result = DreamRunResult(clusters_recorded=3, members_replayed=781, dry_run=False, empty_batches=0)
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation", return_value=result):
            call_command("dream", "run", stdout=stdout)
        assert "returned 0 clusters from non-empty input" not in stdout.getvalue()


class DreamSinceTestCase(TestCase):
    def test_run_passes_since_to_engine(self) -> None:
        captured: dict[str, object] = {}

        def _capture(
            *, overlay: str, since: dt.datetime | None, dry_run: bool, eval_proposals: object = None
        ) -> DreamRunResult:
            captured["since"] = since
            return _ok_result()

        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture):
            call_command("dream", "run", "--since", "2026-06-01T00:00:00+00:00", stdout=StringIO())

        since = captured["since"]
        assert isinstance(since, dt.datetime)
        assert since == dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

    def test_naive_since_is_normalized_to_aware(self) -> None:
        # `--since 2026-06-01` (no tz) would flow into the USE_TZ engine as a
        # naive datetime and TypeError on comparison with timezone.now().
        captured: dict[str, object] = {}

        def _capture(
            *, overlay: str, since: dt.datetime | None, dry_run: bool, eval_proposals: object = None
        ) -> DreamRunResult:
            captured["since"] = since
            return _ok_result()

        with patch("teatree.loops.dream.engine.run_consolidation", side_effect=_capture):
            call_command("dream", "run", "--since", "2026-06-01", stdout=StringIO())

        since = captured["since"]
        assert isinstance(since, dt.datetime)
        assert timezone.is_aware(since)

    def test_malformed_since_raises_command_error(self) -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation") as engine,
            pytest.raises(CommandError),
        ):
            call_command("dream", "run", "--since", "not-a-date", stdout=StringIO())
        engine.assert_not_called()
