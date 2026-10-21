"""Read-only Codex turns record their phase results through the worker."""

import contextlib
import json
import tempfile
from collections.abc import AsyncGenerator, Generator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness_dispatch as harness_dispatch_mod
import teatree.agents.runner as runner_mod
from teatree.agents import codex_app_server_options, codex_read_only_server, harness_registry
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessCapabilities,
    HarnessFallbackError,
    HarnessFallbackKind,
    HarnessSpec,
    register_harness,
)
from teatree.agents.runner import TaskUsage, run_agent
from teatree.agents.skill_routing import clear_route_availability_cache
from teatree.config import get_effective_settings
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import (
    AgentRouteAvailability,
    AutoReviewDispatch,
    CriticDispatch,
    CriticVerdict,
    LandscapeArtifact,
    ReviewVerdict,
    Session,
    Task,
    TaskAttempt,
)
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.types import SkillMetadata
from teatree.utils import run, secrets
from tests.factories import planned_ticket
from tests.teatree_agents._executor_fakes import EXECUTOR_ID, FakeExecutor
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream

_ISSUE_URL = "https://github.com/example-org/example-repo/issues/9"
_SURVEY = {"open_prs": [], "worktrees": [], "recommendations": [], "warnings": []}
_PLAN = {
    "summary": "planned in the executor",
    "plan_text": "## Plan\n1. Change the bar.\n2. Test it.",
    "base_sha": "0" * 40,
    "adequacy": {
        "design": {"content": "route planning through the executor"},
        "integration_seams": {"content": ["src/teatree/agents/harness_registry.py"]},
        "edge_cases": {"content": ["no executor running"]},
        "test_strategy": {"content": "a fake docker replays the executor"},
        "acceptance_criteria": {"content": ["planning runs in the executor"]},
    },
}


class _Harness:
    def __init__(self, *, metered: bool = True) -> None:
        self.capabilities = HarnessCapabilities(metered_lane=metered)

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncGenerator[FakeHarnessSession]:
        del options
        yield FakeHarnessSession(success_stream({"summary": "claude planned instead", "plan_text": ""}))


class _QuotaSession(FakeHarnessSession):
    async def query(self, prompt: str) -> None:
        del prompt
        message = "ChatGPT quota exhausted"
        raise HarnessFallbackError(message, kind=HarnessFallbackKind.QUOTA)


class _QuotaHarness:
    capabilities = HarnessCapabilities(read_only_venue=True)

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncGenerator[FakeHarnessSession]:
        del options
        yield _QuotaSession([])


class TestPlanningOnTheReadOnlyExecutor(TestCase):
    def setUp(self) -> None:
        clear_route_availability_cache()
        self.built: list[str] = []
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fake = FakeExecutor(directory, reply=json.dumps(_PLAN))
        self.env = self.fake.install()
        self.enterContext(self.fake.running())

    def tearDown(self) -> None:
        codex_read_only_server.reset_executor_app_servers()
        harness_registry._REGISTRY.pop("claude_like", None)
        clear_route_availability_cache()

    def _task(self) -> Task:
        ticket = planned_ticket(issue_url=_ISSUE_URL)
        LandscapeArtifact.record(ticket=ticket, survey=_SURVEY, recorded_by="t3:intake")
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="planning")

    def _run(self, *harnesses: str, issue: dict[str, object] | None = None, task: Task | None = None) -> TaskAttempt:
        config = AgentConfig(
            skill_models={
                "architecture-design": tuple(AgentRouteCandidate(name, f"{name}-model") for name in harnesses)
            }
        )
        host = SimpleNamespace(
            get_issue=lambda url: issue if issue is not None else {"body": "Plan the executor."},
            list_issue_comments=lambda *, issue_url: [],
        )
        with self._world(config, host):
            return run_agent(task or self._task(), phase="planning", overlay_skill_metadata=SkillMetadata())

    @contextlib.contextmanager
    def _world(self, config: AgentConfig, host: SimpleNamespace, *, skills: list[str] | None = None) -> Generator[None]:
        with (
            patch.dict("os.environ", {**self.env, "T3_CODEX_HOME": str(self.fake.root / "worker-codex-home")}),
            patch.object(secrets, "run_bounded_group", run.run_bounded_group),
            patch.object(codex_app_server_options, "container_is_the_sandbox", lambda: True),
            patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=True),
            patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
            patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config),
            patch(
                "teatree.agents.runner_skill_staging.resolve_skill_bundle",
                return_value=skills or ["architecture-design"],
            ),
            patch(
                "teatree.agents.read_only_venue_brief.get_backend_provider",
                return_value=SimpleNamespace(get_code_host_for_url=lambda overlay, url: host),
            ),
            patch("teatree.agents.read_only_venue_brief.get_overlay_for_ticket", return_value="example-overlay"),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            yield

    def _register_claude(self) -> None:
        def build(context: HarnessBuildContext) -> _Harness:
            del context
            self.built.append("claude_like")
            return _Harness()

        register_harness(
            HarnessSpec(
                name="claude_like",
                factory=build,
                capabilities=HarnessCapabilities(metered_lane=True),
                allows_provider=False,
            )
        )

    def test_planning_runs_in_the_executor_and_records_its_plan(self) -> None:
        attempt = self._run("codex_app_server")

        assert (attempt.exit_code, attempt.selected_harness, attempt.error) == (0, "codex_app_server", "")
        assert (
            PlanArtifact.objects.filter(ticket=attempt.task.ticket).order_by("-pk").first().plan_text
            == _PLAN["plan_text"]
        )
        launches = [call for call in self.fake.calls if call[:2] == ["exec", "-i"] and "setsid" in call]
        assert [call[call.index("setsid") - 1] for call in launches] == [EXECUTOR_ID]
        (thread_start,) = self.fake.thread_starts
        assert thread_start["sandbox"] == "danger-full-access"
        assert self.fake.keeper_alive

    def test_a_refused_read_only_brief_records_failure_and_parks_without_spending(self) -> None:
        self._register_claude()

        before = timezone.now()
        attempt = self._run("codex_app_server", "claude_like", issue={"error": "Not a GitHub issue URL"})

        attempt.task.refresh_from_db()
        assert attempt.task.status == Task.Status.PENDING
        assert attempt.task.not_before > before
        assert "the issue body could not be read" in attempt.error
        assert "agent_route_paid_fallback" in attempt.error
        assert AgentRouteAvailability.objects.filter(harness="codex_app_server").exclude(unavailable_reason="").exists()
        assert not attempt.task.attempts.filter(lane=TaskAttempt.Lane.METERED).exists()
        assert self.built == []
        assert not [call for call in self.fake.calls if "setsid" in call]

    def test_a_refused_brief_tries_a_later_free_candidate(self) -> None:
        register_harness(
            HarnessSpec(
                name="other_free",
                factory=lambda _context: _Harness(metered=False),
                capabilities=HarnessCapabilities(),
                allows_provider=False,
            )
        )
        self.addCleanup(harness_registry._REGISTRY.pop, "other_free", None)

        attempt = self._run("codex_app_server", "other_free", issue={"error": "Not a GitHub issue URL"})

        assert attempt.selected_harness == "other_free"
        assert attempt.lane != TaskAttempt.Lane.METERED

    def test_a_read_only_run_that_fails_mid_run_is_recorded_and_held_then_parks_at_the_metered_gate(self) -> None:
        self._register_claude()
        register_harness(HarnessSpec(name="quota_ro", factory=lambda _context: _QuotaHarness(), allows_provider=False))
        self.addCleanup(harness_registry._REGISTRY.pop, "quota_ro", None)

        attempt = self._run("quota_ro", "claude_like")

        attempt.task.refresh_from_db()
        failed = attempt.task.attempts.get(selected_harness="quota_ro")
        assert "ChatGPT quota exhausted" in failed.error
        assert AgentRouteAvailability.objects.filter(harness="quota_ro").exclude(unavailable_reason="").exists()
        assert attempt.task.status == Task.Status.PENDING
        assert attempt.task.not_before > timezone.now()
        assert not attempt.task.attempts.filter(lane=TaskAttempt.Lane.METERED).exists()
        assert self.built == []

    def test_third_read_only_refusal_fails_with_a_readable_reason(self) -> None:
        task = self._task()

        first = self._run("codex_app_server", issue={"error": "Not a GitHub issue URL"}, task=task)
        second = self._run("codex_app_server", issue={"error": "Not a GitHub issue URL"}, task=task)
        third = self._run("codex_app_server", issue={"error": "Not a GitHub issue URL"}, task=task)

        task.refresh_from_db()
        assert first.error.startswith("limit_parked:")
        assert second.error.startswith("limit_parked:")
        assert task.status == Task.Status.FAILED
        assert "issue body could not be read" in third.error
        assert "agent_route_paid_fallback" not in third.error

    def test_enabled_paid_fallback_runs_after_read_only_refusal(self) -> None:
        self._register_claude()
        settings = replace(get_effective_settings(None), agent_route_paid_fallback=True)

        with (
            patch.object(harness_dispatch_mod, "get_effective_settings", return_value=settings),
            patch.object(runner_mod, "notify_user"),
        ):
            attempt = self._run("codex_app_server", "claude_like", issue={"error": "Not a GitHub issue URL"})

        assert self.built == ["claude_like"]
        assert attempt.lane == TaskAttempt.Lane.METERED
        assert attempt.route_candidate_index == 1

    def test_remote_review_runs_on_codex_and_records_a_verdict_at_the_dispatched_head(self) -> None:
        head = "a" * 40
        pr_url = "https://github.com/example-org/example-repo/pull/5"
        dispatch = AutoReviewDispatch.enqueue(
            slug="example-org/example-repo",
            pr_id=5,
            head_sha=head,
            pr_url=pr_url,
            overlay="teatree",
        )
        assert dispatch is not None
        assert dispatch.task is not None
        task = dispatch.task
        task.claim(claimed_by="headless-reviewer")
        self.fake.reply = json.dumps(
            {
                "summary": "Reviewed the dispatched pull request head.",
                "review_context": {
                    "work_item": pr_url,
                    "documents": ["src/example.py"],
                    "analysis": "Checked the complete worker diff.",
                },
                "review_verdict": {
                    "verdict": "merge_safe",
                    "reviewed_sha": head,
                    "reviewer_identity": "cold-reviewer-agent",
                    "gh_verify_result": "green",
                    "blast_class": "logic",
                    "findings": [],
                },
            }
        )
        self.fake.install()
        config = AgentConfig(skill_models={"review": (AgentRouteCandidate("codex_app_server", "codex-model"),)})
        host = SimpleNamespace(
            get_issue=lambda url: {"body": "Review this pull request."},
            list_issue_comments=lambda *, issue_url: [],
            get_pr_file_diffs=lambda *, repo, pr_iid: {"src/example.py": "@@ -1 +1 @@\n-old\n+new"},
        )
        with (
            self._world(config, host, skills=["review"]),
            patch(
                "teatree.agents.read_only_venue_brief.live_head_at",
                return_value=SimpleNamespace(sha=head, unreadable=False),
            ),
            patch(
                "teatree.agents.read_only_venue_brief.live_checks_at",
                return_value=SimpleNamespace(status="green", detail="CI green"),
            ),
        ):
            attempt = run_agent(task, phase="reviewing", overlay_skill_metadata=SkillMetadata())

        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert ReviewVerdict.objects.filter(
            slug="example-org/example-repo", pr_id=5, reviewed_sha=head, verdict="merge_safe"
        ).exists()

    def test_critic_runs_on_codex_and_records_a_verdict_at_the_dispatched_head(self) -> None:
        head = "b" * 40
        ticket = planned_ticket(issue_url=_ISSUE_URL)
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha=head, contract="c")
        assert dispatch is not None
        assert dispatch.task is not None
        task = dispatch.task
        task.claim(claimed_by="headless-critic")
        self.fake.reply = json.dumps(
            {
                "summary": "Critic reviewed the dispatched head.",
                "critic_verdict": {
                    "grader_identity": "critic-agent-7",
                    "items": [{"slug": "coherence", "status": "fail", "citation": "src/example.py:1"}],
                },
            }
        )
        self.fake.install()
        config = AgentConfig(skill_models={"review": (AgentRouteCandidate("codex_app_server", "codex-model"),)})
        host = SimpleNamespace(
            get_issue=lambda url: {"body": "Critic review."}, list_issue_comments=lambda *, issue_url: []
        )

        with self._world(config, host, skills=["review"]):
            attempt = run_agent(task, phase="critic_reviewing", overlay_skill_metadata=SkillMetadata())

        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert CriticVerdict.objects.filter(ticket=ticket, transition="mark_delivered", head_sha=head).exists()
