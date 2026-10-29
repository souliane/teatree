"""A t3-teatree dispatch, with the overlay's REAL skill metadata, delivers only real skills."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from teatree.agents._runner_env import DispatchCredential
from teatree.agents.harness import ClaudeSdkHarness
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.prompt import build_system_context
from teatree.agents.runner_preparation import Preflight, PreparedRun, prepare_run
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.config import cold_reader
from teatree.config.cold_reader import SettingRead
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core.models import Session, Task, Worktree
from teatree.skill_support.loading import SkillLoadingPolicy
from tests._git_repo import make_git_repo, run_git
from tests.factories import planned_ticket

_TEATREE_CHECKOUT = Path(__file__).resolve().parents[2]
_DISPATCHED_PHASES = ("coding", "codex_reviewing", "codex_adversarial_reviewing")


class TestT3TeatreeSkillDispatch(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = planned_ticket()

    def _task(self, phase: str) -> Task:
        return Task.objects.create(ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase=phase)

    def _bundle(self, phase: str, worktree: Path) -> list[str]:
        return resolve_skill_bundle(
            phase=phase,
            overlay_skill_metadata=TeatreeOverlay().metadata.get_skill_metadata(),
            detection_root=worktree,
            stage_skills=[],
        )

    def _prepare(self, phase: str, worktree: Path) -> PreparedRun:
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities()),
            name="codex_app_server",
            provider=None,
            model=None,
            route_candidate_index=None,
        )
        preflight = Preflight(stage_skills=[], dispatch=dispatch, skills=self._bundle(phase, worktree))
        return prepare_run(self._task(phase), preflight, phase=phase, handoff=None, credential=DispatchCredential())

    def test_dispatch_requests_no_phantom_skill(self) -> None:
        overlay_skill_path = TeatreeOverlay().metadata.get_skill_metadata()["skill_path"]
        for phase in _DISPATCHED_PHASES:
            with self.subTest(phase=phase):
                assert overlay_skill_path in self._bundle(phase, _TEATREE_CHECKOUT)
                assurance = self._prepare(phase, _TEATREE_CHECKOUT).skill_assurance

                assert assurance["missing"] == []
                assert "teatree" not in assurance["requested"]
                assert "skills" not in assurance["requested"]

    def test_the_overlay_skill_path_itself_delivers_internals_outside_a_checkout(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        assert SkillLoadingPolicy.detect_internals_skill(outside) == []

        assurance = self._prepare("coding", outside).skill_assurance

        assert "internals" in assurance["requested"]
        assert assurance["missing"] == []

    def test_codex_review_embeds_internals_once_when_detector_and_overlay_both_name_it(self) -> None:
        phase = "codex_reviewing"
        context = build_system_context(self._task(phase), skills=self._bundle(phase, _TEATREE_CHECKOUT))

        assert context.count("--- SKILL: internals ---") == 1

    def test_coding_stack_load_block_lists_internals_once(self) -> None:
        prompt = self._prepare("coding", _TEATREE_CHECKOUT).prompt

        assert prompt.splitlines().count("  - /t3:internals") == 1

    def test_coding_dispatch_records_the_round_start_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_git_repo(Path(raw))
            Worktree.objects.create(
                ticket=self.ticket,
                repo_path=str(repo),
                branch="main",
                extra={"worktree_path": str(repo)},
            )
            self._prepare("coding", repo)
            self.ticket.refresh_from_db(fields=["extra"])
            assert list(self.ticket.extra["brief_start_heads"].values()) == [run_git(repo, "rev-parse", "HEAD")]

    def test_retry_keeps_the_first_round_start_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = make_git_repo(Path(raw))
            Worktree.objects.create(
                ticket=self.ticket,
                repo_path=str(repo),
                branch="main",
                extra={"worktree_path": str(repo)},
            )
            task = self._task("coding")
            dispatch = DispatchHarness(
                harness=SimpleNamespace(capabilities=HarnessCapabilities()),
                name="codex_app_server",
                provider=None,
                model=None,
                route_candidate_index=None,
            )
            preflight = Preflight(stage_skills=[], dispatch=dispatch, skills=self._bundle("coding", repo))
            start = run_git(repo, "rev-parse", "HEAD")
            prepare_run(task, preflight, phase="coding", handoff=None, credential=DispatchCredential())
            run_git(repo, "commit", "--allow-empty", "-m", "second")
            prepare_run(task, preflight, phase="coding", handoff=None, credential=DispatchCredential())
            self.ticket.refresh_from_db(fields=["extra"])
            assert self.ticket.extra["brief_start_heads"][str(task.pk)] == start

    def test_claude_child_keeps_native_compaction_available(self) -> None:
        dispatch = DispatchHarness(
            harness=ClaudeSdkHarness(),
            name="claude_sdk",
            provider=None,
            model="claude-opus-5-5",
            route_candidate_index=None,
        )
        prepared = prepare_run(
            self._task("coding"),
            Preflight(stage_skills=[], dispatch=dispatch, skills=[]),
            phase="coding",
            handoff=None,
            credential=DispatchCredential(env={"ANTHROPIC_API_KEY": "key-x"}),
        )

        assert prepared.options.env["DISABLE_COMPACT"] == "0"
        assert prepared.options.env["DISABLE_AUTO_COMPACT"] == "0"
        assert "PreCompact" not in (prepared.options.hooks or {})
        assert prepared.options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"

    def test_claude_child_with_an_explicit_window_gets_the_reserve_percentage(self) -> None:
        dispatch = DispatchHarness(
            harness=ClaudeSdkHarness(),
            name="claude_sdk",
            provider=None,
            model="claude-sonnet-4-6",
            route_candidate_index=None,
        )
        original_read = cold_reader.read_setting_confirmed

        def read_window(key: str, **kwargs: object) -> SettingRead:
            if key == "agent_model_context_windows":
                return SettingRead(value={"claude-sonnet-4-6": 200_000}, readable=True)
            return original_read(key, **kwargs)

        with patch("teatree.agents.runner_preparation.cold_reader.read_setting_confirmed", side_effect=read_window):
            prepared = prepare_run(
                self._task("coding"),
                Preflight(stage_skills=[], dispatch=dispatch, skills=[]),
                phase="coding",
                handoff=None,
                credential=DispatchCredential(
                    env={
                        "ANTHROPIC_API_KEY": "key-x",
                        "DISABLE_COMPACT": "1",
                        "DISABLE_AUTO_COMPACT": "true",
                        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
                        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "25",
                    }
                ),
            )

        assert prepared.options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
        assert prepared.options.env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "92.0"
        assert prepared.options.env["DISABLE_COMPACT"] == "0"
        assert prepared.options.env["DISABLE_AUTO_COMPACT"] == "0"

    def test_claude_child_overrides_ambient_compaction_disable_switches(self) -> None:
        dispatch = DispatchHarness(
            harness=ClaudeSdkHarness(),
            name="claude_sdk",
            provider=None,
            model="claude-opus-5-5",
            route_candidate_index=None,
        )
        with patch.dict("os.environ", {"DISABLE_COMPACT": "1", "DISABLE_AUTO_COMPACT": "true"}):
            prepared = prepare_run(
                self._task("coding"),
                Preflight(stage_skills=[], dispatch=dispatch, skills=[]),
                phase="coding",
                handoff=None,
                credential=DispatchCredential(env={"ANTHROPIC_API_KEY": "key-x"}),
            )

        assert prepared.options.env["DISABLE_COMPACT"] == "0"
        assert prepared.options.env["DISABLE_AUTO_COMPACT"] == "0"
