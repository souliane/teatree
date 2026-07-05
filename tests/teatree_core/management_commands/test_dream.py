"""``manage.py dream`` — orchestration for the idle-time dream cron (#1933).

The command owns the cron mechanics around the (stubbed) distillation engine:
the in-flight ``LoopLease`` lock so two passes never overlap, the cadence gate
for ``tick``, the ``DreamRunMarker`` stamping (success vs. attempt), and the
``--dry-run`` no-write path. These are all testable without an LLM because the
engine is a typed seam.
"""

import datetime as dt
import tempfile
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConsolidatedMemory, DreamRunMarker, Loop, LoopLease
from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, DreamRunResult, TranscriptMember
from teatree.loops.dream.loop import DREAM_LEASE_NAME, DREAM_LEASE_SECONDS, DREAM_LOOP_NAME


def _enable_dream_loop(*, last_run_at: "dt.datetime | None" = None) -> None:
    """Seed an ENABLED, interval-cadenced ``dream`` Loop row — the ONE cadence ledger.

    ``last_run_at=None`` ⇒ due immediately; a recent ``last_run_at`` ⇒ not due.
    """
    Loop.objects.update_or_create(
        name=DREAM_LOOP_NAME,
        defaults={
            "script": "src/teatree/loops/dream/loop.py",
            "prompt": None,
            "delay_seconds": 86400,
            "daily_at": None,
            "enabled": True,
            "last_run_at": last_run_at,
        },
    )


class _DreamTickEnabledMixin:
    """Mixin for tests that drive ``dream tick`` to RUN.

    The dream Loop row ships PAUSED (#2513) and the cron gate now routes through the
    single enable verdict (``Loop.enabled`` + ``LoopState``), so a tick SKIPs until
    the row is enabled. These tests enable an interval-cadenced, due dream row.
    """

    def setUp(self) -> None:
        super().setUp()
        _enable_dream_loop(last_run_at=None)


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


class DreamNightlyTickRequestsProposalsTestCase(_DreamTickEnabledMixin, TestCase):
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


class DreamDeriveEvalsWiringTestCase(_DreamTickEnabledMixin, TestCase):
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


class DreamLiveValidationGateWiringTestCase(_DreamTickEnabledMixin, TestCase):
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
        _enable_dream_loop(last_run_at=None)  # dream ships paused; tick gates on the enabled row

    #: All phase toggles cleared to default-ON unless a test overrides one.
    _PHASE_ENV: ClassVar[dict[str, str]] = {
        "T3_DREAM_PROPOSE_EVALS": "",
        "T3_DREAM_CROSS_LINK": "",
        "T3_DREAM_MERGE": "",
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

    def test_merge_phase_collapses_near_duplicates_and_index_shrinks(self) -> None:
        # Two NEAR-DUPLICATE feedback files (Jaccard >= 0.85, same family) collapse
        # to one survivor; the index lists one fewer pointer afterwards (#2723).
        topic = (
            "the followup loop pull reminder cadence nag interval threshold escalation "
            "stale open review request daily digest batch surfacing notify channel dm "
            "merge clearance approval gate pipeline status watch tick orchestrator dispatch"
        )
        (self.memdir / "feedback_dup_a.md").write_text(
            f"---\nname: feedback_dup_a\ntype: feedback\n---\n{topic} FIRST distinct detail\n", encoding="utf-8"
        )
        (self.memdir / "feedback_dup_b.md").write_text(
            f"---\nname: feedback_dup_b\ntype: feedback\n---\n{topic} SECOND distinct detail\n", encoding="utf-8"
        )
        stdout = StringIO()
        self._tick(stdout)
        out = stdout.getvalue()
        assert "merged" in out
        survivors = {p.name for p in self.memdir.glob("feedback_dup_*.md")}
        assert len(survivors) == 1
        # The merged-away file is archived (moved), never deleted.
        assert (self.memdir / "archive").is_dir()
        # The re-index dropped the absorbed pointer: index lists one feedback_dup file.
        index = (self.memdir / "MEMORY.md").read_text(encoding="utf-8")
        assert index.count("feedback_dup_") == 1

    def test_merge_disabled_keeps_both_near_duplicates(self) -> None:
        topic = (
            "the followup loop pull reminder cadence nag interval threshold escalation "
            "stale open review request daily digest batch surfacing notify channel dm "
            "merge clearance approval gate pipeline status watch tick orchestrator dispatch"
        )
        (self.memdir / "feedback_dup_a.md").write_text(
            f"---\nname: feedback_dup_a\ntype: feedback\n---\n{topic} FIRST\n", encoding="utf-8"
        )
        (self.memdir / "feedback_dup_b.md").write_text(
            f"---\nname: feedback_dup_b\ntype: feedback\n---\n{topic} SECOND\n", encoding="utf-8"
        )
        stdout = StringIO()
        self._tick(stdout, env={"T3_DREAM_MERGE": "0"})
        assert "merged" not in stdout.getvalue()
        assert (self.memdir / "feedback_dup_a.md").exists()
        assert (self.memdir / "feedback_dup_b.md").exists()

    def _write_binding_conflict(self) -> None:
        # Isolate: drop the setUp memories so only the binding pair is present.
        (self.memdir / "mem_a.md").unlink()
        (self.memdir / "mem_b.md").unlink()
        topic = (
            "the followup loop pull reminder cadence nag interval threshold escalation "
            "stale open review request daily digest batch surfacing notify channel dm "
            "merge clearance approval gate pipeline status watch tick orchestrator dispatch"
        )
        (self.memdir / "feedback_bind_one.md").write_text(
            f"---\nname: feedback_bind_one\ntype: feedback\n---\n{topic} BINDING always push first\n", encoding="utf-8"
        )
        (self.memdir / "feedback_bind_two.md").write_text(
            f"---\nname: feedback_bind_two\ntype: feedback\n---\n{topic} BINDING never push first\n", encoding="utf-8"
        )

    def test_two_binding_conflicts_file_a_reconciliation_ticket(self) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.core.backend_protocols import CodeHostBackend  # noqa: PLC0415

        self._write_binding_conflict()
        host = MagicMock(spec=CodeHostBackend)
        host.search_open_issues.return_value = []
        host.create_issue.return_value = {"html_url": "https://github.com/souliane/teatree/issues/9100"}
        stdout = StringIO()
        with patch(
            "teatree.core.management.commands.dream.Command._teatree_backlog_host",
            return_value=(host, "souliane/teatree"),
        ):
            self._tick(stdout)
        # The two BINDING files were NOT merged; a reconciliation ticket was filed.
        assert "merged" not in stdout.getvalue()
        assert "filed 1 binding-reconciliation ticket" in stdout.getvalue()
        assert (self.memdir / "feedback_bind_one.md").exists()
        assert (self.memdir / "feedback_bind_two.md").exists()
        host.create_issue.assert_called_once()

    def test_binding_conflict_with_no_host_is_warned_not_crashed(self) -> None:
        self._write_binding_conflict()
        stdout = StringIO()
        with patch(
            "teatree.core.management.commands.dream.Command._teatree_backlog_host",
            return_value=(None, "souliane/teatree"),
        ):
            self._tick(stdout)
        assert "no teatree code host resolved" in stdout.getvalue()
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None

    def test_merge_phase_failure_is_warned_not_crashed(self) -> None:
        stdout = StringIO()
        with patch("teatree.loops.dream.merge.merge_memories", side_effect=RuntimeError("merge boom")):
            self._tick(stdout)
        out = stdout.getvalue()
        assert "WARN merge raised: RuntimeError" in out
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None

    def test_budget_tier_archives_duplicates_and_index_drops_under_budget(self) -> None:
        # #2723 anti-vacuous end-to-end: an over-budget index built from >90d
        # near-duplicate files -> decay archives >0 AND MEMORY.md falls back under
        # the gate-(d) load budget in the same pass.
        import os  # noqa: PLC0415
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        from teatree.loops.dream import gates  # noqa: PLC0415

        (self.memdir / "mem_a.md").unlink()
        (self.memdir / "mem_b.md").unlink()
        old = (datetime.now(tz=UTC) - timedelta(days=120)).timestamp()
        topic = (
            "the followup loop pull reminder cadence nag interval threshold escalation "
            "stale open review request daily digest batch surfacing notify channel dm "
            "merge clearance approval gate pipeline status watch tick orchestrator dispatch"
        )
        # Many >90d near-duplicate feedback files (pairs of the same lesson) so the
        # rendered index is well over the ~24 KB session-load byte budget.
        for i in range(180):
            for half in ("a", "b"):
                f = self.memdir / f"feedback_dup_{i:03d}_{half}.md"
                f.write_text(
                    f"---\nname: feedback_dup_{i:03d}_{half}\ntype: feedback\n---\n{topic} lesson {i}\n",
                    encoding="utf-8",
                )
                os.utime(f, (old, old))
        stdout = StringIO()
        # Isolate the budget tier: cross-link OFF (it would link all near-duplicates
        # and mark them referenced, which #2723 §2(d) calls out as the deadlock the
        # tier must not be defeated by) and merge OFF (so the tier, not merge, prunes).
        self._tick(stdout, env={"T3_DREAM_CROSS_LINK": "0", "T3_DREAM_MERGE": "0"})
        out = stdout.getvalue()
        assert "archived" in out
        # MEMORY.md is now under the gate-(d) load budget.
        snap = gates.snapshot_memory_dir(self.memdir)
        assert gates.Gate.index_budget(snap).passed, snap.index_byte_size

    def test_binding_reconciliation_failure_is_warned_not_crashed(self) -> None:
        self._write_binding_conflict()
        stdout = StringIO()
        with (
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
            patch(
                "teatree.loops.dream.promote_memory.file_binding_reconciliation_tickets",
                side_effect=RuntimeError("reconcile boom"),
            ),
        ):
            self._tick(stdout)
        out = stdout.getvalue()
        assert "WARN binding reconciliation raised: RuntimeError" in out
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None

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
        _enable_dream_loop(last_run_at=None)  # dream ships paused; tick gates on the enabled row

    def _run(self, stdout: StringIO, *, report: "DreamQaReport") -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch("teatree.loops.dream.acceptance.run_acceptance_pass", return_value=report),
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
        _enable_dream_loop(last_run_at=None)  # dream ships paused; tick gates on the enabled row

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


class DreamPriorArchivedPointerStampsSucceededTestCase(TestCase):
    """A quiet 0-cluster pass dropping a stale prior-archived pointer stamps success (#2545).

    A memory archived a prior pass lives in ``archive/`` (lesson preserved + recall-able),
    but the on-disk ``MEMORY.md`` still carried a stale pointer to it. The re-index drops
    that pointer this pass. The §4 consolidation gate must home the pruned pointer against
    the durable ``archive/`` cold store — not flag it as a lost prune — so the marker is
    stamped and the >48h staleness alarm clears. Before the fix the gate homed only THIS
    pass's archives, so the pruned pointer looked unhomed, gate (c) FAILED, and the marker
    was starved every quiet night ("acceptance gate(s) FAILED — marker NOT stamped"). The
    memory dir is a TMP fixture; ``~/.claude`` is never touched.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        self.memdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        topic = "the worktree provision lease pid claim guard owner liveness anchored"
        (self.memdir / "mem_a.md").write_text(f"name: mem_a\n{topic}\n", encoding="utf-8")
        (self.memdir / "mem_b.md").write_text(f"name: mem_b\n{topic} session\n", encoding="utf-8")
        archive = self.memdir / "archive"
        archive.mkdir()
        (archive / "feedback_gamma.md").write_text(
            "ARCHIVED\n---\ndescription: a gamma lesson archived a prior pass\n---\nbody gamma\n",
            encoding="utf-8",
        )
        # The on-disk index still references the prior-archived gamma (a stale pointer).
        (self.memdir / "MEMORY.md").write_text(
            "# Auto Memory — Index\n\n"
            "> Generated by the dream re-index phase. One line per memory; detail lives in the topic file. "
            "Do not move content into this index.\n\n"
            "- mem_a.md — a topic\n- mem_b.md — a topic session\n"
            "- feedback_gamma.md — a gamma lesson archived a prior pass\n",
            encoding="utf-8",
        )
        _enable_dream_loop(last_run_at=None)

    def test_stale_prior_archived_pointer_does_not_starve_the_marker(self) -> None:
        stdout = StringIO()
        zero_cluster = DreamRunResult(clusters_recorded=0, members_replayed=976, dry_run=False)
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=zero_cluster),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch.dict(
                "os.environ",
                {
                    "T3_DREAM_PROPOSE_EVALS": "",
                    "T3_DREAM_CROSS_LINK": "0",
                    "T3_DREAM_MERGE": "0",
                    "T3_DREAM_DECAY": "0",
                },
                clear=False,
            ),
        ):
            __import__("os").environ.pop("T3_DREAM_REINDEX", None)  # re-index ON: drops the stale pointer
            call_command("dream", "tick", stdout=stdout)
        out = stdout.getvalue()
        assert "re-indexed" in out  # real maintenance happened
        assert "acceptance gate(s) FAILED" not in out
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None
        assert DreamRunMarker.objects.is_stale(timezone.now()) is False


class DreamConsolidatesRawTranscriptLearningTestCase(TestCase):
    """A raw transcript learning reaches the distiller, grounds, passes the gates, and stamps (#2986).

    The input-starvation regression, end-to-end through the command with the REAL
    engine + REAL §4 gates + REAL marker (only the LLM distiller and the member
    enumeration are faked). A realistic session transcript carries a substantive
    finding that holds NONE of the literal signal tokens and neither a correction
    nor an ask cue. Before the fix the keyword gate dropped it, the extract was
    empty, the distiller was never called, 0 clusters were recorded, the §4
    consolidation gate FAILED, and the DreamRunMarker was never stamped succeeded
    (the >48h staleness alarm never cleared). The file-side phases are OFF so the
    ONLY path to a passing consolidation gate is a genuinely-grounded cluster — the
    gate stays anti-vacuous. The memory dir + transcript are TMP; ``~/.claude`` is
    never touched.
    """

    def setUp(self) -> None:
        self.memdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        topic = "the worktree provision lease pid claim guard owner liveness anchored"
        (self.memdir / "mem_a.md").write_text(f"name: mem_a\n{topic}\n", encoding="utf-8")
        session_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.jsonl = session_dir / "session-xyz.jsonl"
        chatter = "\n".join(f'{{"type":"assistant","text":"computed result row {i}"}}' for i in range(30))
        self.finding = "root caused the empty owner crash to a missing tenant filter in resolve_owner"
        self.jsonl.write_text(chatter + "\n" + f'{{"type":"assistant","text":"{self.finding}"}}', encoding="utf-8")

    def _run(self, stdout: StringIO) -> None:
        member = TranscriptMember(path=self.jsonl, kind="main")

        def _distill(extract: ConsolidationExtract) -> list[DistilledCluster]:
            snippet = next(s for s in extract.snippets if s.kind != "memory")
            return [
                DistilledCluster(
                    cluster_key="raw-learning",
                    rule="Guard resolve_owner against a missing tenant filter.",
                    source_files=[str(snippet.path)],
                    is_binding=False,
                    verified_citation=self.finding,
                    durable_destination="",
                )
            ]

        with (
            patch("teatree.loops.dream.engine.enumerate_members", return_value=[member]),
            patch("teatree.loops.dream.sdk_distiller.sdk_distill", side_effect=_distill),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir]),
            patch.dict(
                "os.environ",
                {
                    "T3_DREAM_PROPOSE_EVALS": "0",
                    "T3_DREAM_CROSS_LINK": "0",
                    "T3_DREAM_MERGE": "0",
                    "T3_DREAM_REINDEX": "0",
                    "T3_DREAM_DECAY": "0",
                },
                clear=False,
            ),
        ):
            call_command("dream", "run", stdout=stdout)

    def test_raw_learning_grounds_a_cluster_passes_gates_and_stamps_marker(self) -> None:
        stdout = StringIO()
        self._run(stdout)
        out = stdout.getvalue()
        # (1) genuinely consolidated real learnings — one grounded cluster recorded.
        assert ConsolidatedMemory.objects.filter(cluster_key="raw-learning").count() == 1
        assert "1 cluster(s) recorded" in out
        # (2) passed acceptance on real non-empty input (no gate laundering).
        assert "all acceptance gates passed" in out
        assert "acceptance gate(s) FAILED" not in out
        # (3) advanced the cadence marker — the >48h staleness alarm cleared.
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at is not None
        assert DreamRunMarker.objects.is_stale(timezone.now()) is False


class DreamMemoryPromotionWiringTestCase(_DreamTickEnabledMixin, TestCase):
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
            patch("teatree.loops.dream.umbrella_ledger.reconcile_merged_gaps", return_value=[]),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
        ):
            self._tick(stdout, env={"T3_DREAM_MEMORY_PROMOTE": "1"})
        assert "promoted 1 core-gap fix(es), reconciled 0 merged" in stdout.getvalue()

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


class DreamAutomationAsksWiringTestCase(_DreamTickEnabledMixin, TestCase):
    """Phase-3d automatable-ask promotion only runs when its default-OFF toggle is on (#2663)."""

    def _tick(self, stdout: StringIO, *, env: dict[str, str]) -> None:
        environ = {
            "T3_DREAM_PROPOSE_EVALS": "0",
            "T3_DREAM_CROSS_LINK": "0",
            "T3_DREAM_REINDEX": "0",
            "T3_DREAM_MEMORY_PROMOTE": "0",
            "T3_DREAM_COMPLIANCE": "0",
            **env,
        }
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch.dict("os.environ", environ, clear=False),
        ):
            call_command("dream", "tick", stdout=stdout)

    def test_promotion_skipped_when_toggle_off(self) -> None:
        with patch("teatree.loops.dream.automation_ask.run_automation_asks_phase") as phase_fn:
            self._tick(StringIO(), env={"T3_DREAM_AUTOMATION_ASKS": "0"})
        phase_fn.assert_not_called()

    def test_promotion_runs_and_reports_when_toggle_on(self) -> None:
        stdout = StringIO()
        with (
            patch(
                "teatree.loops.dream.automation_ask.run_automation_asks_phase",
                return_value="; promoted 2 automatable-ask fix(es)",
            ) as phase_fn,
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
        ):
            self._tick(stdout, env={"T3_DREAM_AUTOMATION_ASKS": "1"})
        phase_fn.assert_called_once()
        assert "promoted 2 automatable-ask fix(es)" in stdout.getvalue()

    def test_promotion_failure_is_warned_not_crashed(self) -> None:
        stdout = StringIO()
        with (
            patch(
                "teatree.loops.dream.automation_ask.run_automation_asks_phase",
                side_effect=RuntimeError("ask boom"),
            ),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
        ):
            self._tick(stdout, env={"T3_DREAM_AUTOMATION_ASKS": "1"})
        out = stdout.getvalue()
        assert "WARN automatable-ask phase raised: RuntimeError" in out
        assert DreamRunMarker.objects.get(name=DreamRunMarker.NAME).last_succeeded_at is not None

    def test_no_code_host_is_warned_not_crashed(self) -> None:
        stdout = StringIO()
        with patch(
            "teatree.core.management.commands.dream.Command._teatree_backlog_host",
            return_value=(None, "souliane/teatree"),
        ):
            self._tick(stdout, env={"T3_DREAM_AUTOMATION_ASKS": "1"})
        assert "automatable-ask promotion skipped — no teatree code host resolved" in stdout.getvalue()

    def test_full_runs_automation_asks_despite_toggle_off(self) -> None:
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_ok_result()),
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=[]),
            patch("teatree.loops.dream.automation_ask.run_automation_asks_phase", return_value="") as phase_fn,
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(object(), "souliane/teatree"),
            ),
            patch.dict(
                "os.environ",
                {"T3_DREAM_MEMORY_PROMOTE": "0", "T3_DREAM_DERIVE_EVALS": "0", "T3_DREAM_AUTOMATION_ASKS": "0"},
                clear=False,
            ),
        ):
            call_command("dream", "run", "--full", stdout=StringIO())
        phase_fn.assert_called_once()


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
            patch("teatree.loops.dream.umbrella_ledger.reconcile_merged_gaps", return_value=[]),
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
            patch("teatree.loops.dream.umbrella_ledger.reconcile_merged_gaps", return_value=[]),
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
    """The dream cron gates on the ONE cadence ledger (LOOP-PR-A).

    The ``dream`` Loop row's ``is_due`` / ``last_run_at`` plus the single enable
    verdict — never a second cadence-marker ledger.
    """

    def test_tick_runs_when_due_and_bumps_last_run_at(self) -> None:
        _enable_dream_loop(last_run_at=None)  # never run ⇒ due
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ) as engine:
            call_command("dream", "tick", stdout=StringIO())
        engine.assert_called_once()
        assert Loop.objects.get(name=DREAM_LOOP_NAME).last_run_at is not None

    def test_tick_skips_when_not_due(self) -> None:
        _enable_dream_loop(last_run_at=timezone.now())  # just ran ⇒ not due
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation") as engine:
            call_command("dream", "tick", stdout=stdout)
        engine.assert_not_called()
        assert "SKIP" in stdout.getvalue()

    def test_tick_skips_when_loop_disabled(self) -> None:
        # A disabled Loop row (or no row) is a hard skip via the single verdict.
        Loop.objects.update_or_create(
            name=DREAM_LOOP_NAME,
            defaults={
                "script": "src/teatree/loops/dream/loop.py",
                "prompt": None,
                "delay_seconds": 86400,
                "daily_at": None,
                "enabled": False,
                "last_run_at": None,
            },
        )
        stdout = StringIO()
        with patch("teatree.loops.dream.engine.run_consolidation") as engine:
            call_command("dream", "tick", stdout=stdout)
        engine.assert_not_called()
        assert "SKIP" in stdout.getvalue()

    def test_run_ignores_cadence_gate(self) -> None:
        # `run` is the manual escape hatch — it runs regardless of cadence / enable.
        _enable_dream_loop(last_run_at=timezone.now())  # not due
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            return_value=_ok_result(),
        ) as engine:
            call_command("dream", "run", stdout=StringIO())
        engine.assert_called_once()

    def test_tick_failed_engine_does_not_advance_cadence_ledger(self) -> None:
        _enable_dream_loop(last_run_at=None)  # due
        with patch(
            "teatree.loops.dream.engine.run_consolidation",
            side_effect=RuntimeError("engine boom"),
        ):
            call_command("dream", "tick", stdout=StringIO())
        assert Loop.objects.get(name=DREAM_LOOP_NAME).last_run_at is None


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
        _enable_dream_loop(last_run_at=None)  # dream ships paused; tick gates on the enabled row

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
