"""Read-only Codex turns record their phase results through the worker."""

import contextlib
import json
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

import teatree.agents.harness_dispatch as harness_dispatch_mod
import teatree.agents.runner as runner_mod
from teatree.agents import codex_app_server_options, codex_read_only_server, harness_registry
from teatree.agents.harness_registry import HarnessBuildContext, HarnessCapabilities, HarnessSpec, register_harness
from teatree.agents.runner import TaskUsage, run_agent
from teatree.agents.skill_routing import clear_route_availability_cache
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import (
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
    def __init__(self) -> None:
        self.capabilities = HarnessCapabilities()

    @contextlib.asynccontextmanager
    async def open(self, options: object) -> AsyncGenerator[FakeHarnessSession]:
        del options
        yield FakeHarnessSession(success_stream({"summary": "claude planned instead", "plan_text": ""}))


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

    def _run(self, *harnesses: str, issue: dict[str, object] | None = None) -> TaskAttempt:
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
            return run_agent(self._task(), phase="planning", overlay_skill_metadata=SkillMetadata())

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

        register_harness(HarnessSpec(name="claude_like", factory=build, allows_provider=False))

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

    def test_a_refused_read_only_brief_falls_back_to_the_next_route_candidate(self) -> None:
        self._register_claude()

        attempt = self._run("codex_app_server", "claude_like", issue={"error": "Not a GitHub issue URL"})

        assert attempt.selected_harness == "claude_like"
        assert "the issue body could not be read" in attempt.fallback_reason
        assert self.built == ["claude_like"]
        assert not [call for call in self.fake.calls if "setsid" in call]

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
            fetch_pr_message=lambda *, slug, pr_id: SimpleNamespace(body="Review this pull request."),
            list_pr_comments=lambda *, repo, pr_iid: [],
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
