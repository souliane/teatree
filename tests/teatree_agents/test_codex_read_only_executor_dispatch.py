"""Read-only Codex turns record their phase results through the worker."""

import contextlib
import json
import tempfile
from collections.abc import AsyncGenerator, Generator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

import teatree.agents.harness_dispatch as harness_dispatch_mod
import teatree.agents.runner as runner_mod
from teatree.agents import codex_app_server_options, codex_read_only_server, harness_registry
from teatree.agents.harness_registry import HarnessBuildContext, HarnessCapabilities, HarnessSpec, register_harness
from teatree.agents.runner import TaskUsage, run_agent
from teatree.agents.skill_routing import clear_route_availability_cache
from teatree.config import get_effective_settings
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import (
    AgentRouteAvailability,
    AutoReviewDispatch,
    CriticDispatch,
    CriticVerdict,
    Directive,
    DirectiveDispatch,
    LandscapeArtifact,
    PendingTriageRecommendation,
    ReviewVerdict,
    Session,
    Task,
    TaskAttempt,
    Ticket,
)
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.types import SkillMetadata
from teatree.utils import run, secrets
from tests.factories import planned_ticket
from tests.teatree_agents._executor_fakes import EXECUTOR_ID, FakeExecutor
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream
from tests.teatree_core.models.test_mechanism_sketch import default_behaviour_envelope

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


@pytest.mark.usefixtures("pass_store")
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
        assert not AgentRouteAvailability.objects.filter(harness="codex_app_server").exists()
        assert not attempt.task.attempts.filter(lane=TaskAttempt.Lane.METERED).exists()
        assert self.built == []
        assert not [call for call in self.fake.calls if "setsid" in call]

    def test_a_refused_brief_on_an_unrouted_dispatch_fails_with_the_refusal(self) -> None:
        settings = replace(get_effective_settings(None), agent_harness="codex_app_server")

        with patch.object(harness_dispatch_mod, "get_effective_settings", return_value=settings):
            attempt = self._run(issue={"error": "Not a GitHub issue URL"})

        attempt.task.refresh_from_db()
        assert attempt.task.status == Task.Status.FAILED
        assert "the issue body could not be read" in attempt.error

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

        with patch.object(runner_mod, "notify_user") as notify:
            attempt = self._run("codex_app_server", "other_free", issue={"error": "Not a GitHub issue URL"})

        assert attempt.selected_harness == "other_free"
        assert attempt.lane != TaskAttempt.Lane.METERED
        notify.assert_not_called()

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

    def test_third_park_after_a_refusing_codex_and_a_gated_metered_candidate_fails_the_task(self) -> None:
        self._register_claude()
        task = self._task()

        attempts = [
            self._run("codex_app_server", "claude_like", issue={"error": "Not a GitHub issue URL"}, task=task)
            for _ in range(3)
        ]

        task.refresh_from_db()
        assert [attempt.error.startswith("limit_parked:") for attempt in attempts] == [True, True, False]
        assert task.status == Task.Status.FAILED
        assert "issue body could not be read" in attempts[-1].error
        assert "agent_route_paid_fallback" in attempts[-1].error
        assert task.attempts.filter(error__startswith="read-only executor brief refused:").count() == 3
        assert not task.attempts.filter(lane=TaskAttempt.Lane.METERED).exists()
        assert self.built == []

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

    def _run_read_only_phase(
        self, task: Task, *, skill: str, reply: dict[str, object], host: SimpleNamespace
    ) -> TaskAttempt:
        self.fake.reply = json.dumps(reply)
        self.fake.install()
        config = AgentConfig(skill_models={skill: (AgentRouteCandidate("codex_app_server", "codex-model"),)})
        with (
            self._world(config, host, skills=[skill]),
            patch("teatree.agents._runner_options._main_clone_cwd", return_value=None),
            patch("teatree.agents.read_only_venue_brief.fetch_branch"),
        ):
            return run_agent(task, phase=task.phase, overlay_skill_metadata=SkillMetadata())

    def _turn_text(self) -> str:
        requests = [json.loads(line) for line in Path(f"{self.fake.log}.rpc").read_text(encoding="utf-8").splitlines()]
        return json.dumps([request["params"] for request in requests if request.get("method") == "turn/start"])

    def test_triage_assessment_runs_on_codex_and_records_its_recommendations(self) -> None:
        urls = [f"https://github.com/example-org/example-repo/issues/{number}" for number in (31, 32)]
        reason = "ISSUES:\n" + "\n".join(f"- {url} | title | needs-triage" for url in urls)
        ticket = Ticket.objects.create(issue_url="triage-assessor://example-overlay", overlay="example-overlay")
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="triage_assessing",
            execution_reason=reason,
        )
        recommendations = [{"issue_url": url, "verdict": "keep", "rationale": "real"} for url in urls]
        host = SimpleNamespace(
            get_issue=lambda url: {"body": f"Body of {url}"}, list_issue_comments=lambda *, issue_url: []
        )

        attempt = self._run_read_only_phase(
            task,
            skill="triaging-issues",
            reply={"summary": "assessed", "triage_recommendations": recommendations},
            host=host,
        )

        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert all(f"Body of {url}" in self._turn_text() for url in urls)
        assert PendingTriageRecommendation.objects.filter(
            url_hash__in=[PendingTriageRecommendation.hash_url(url) for url in urls]
        ).count() == len(urls)

    def test_directive_interpretation_runs_on_codex_and_records_its_sketch(self) -> None:
        directive = Directive.objects.capture("never retry a hold", source=Directive.Source.CLI)
        row = DirectiveDispatch.enqueue(directive=directive, contract="Directive (verbatim):\nnever retry a hold")
        assert row is not None
        assert row.task is not None
        reply = {"summary": "interpreted", "directive_interpretation": {"sketch": default_behaviour_envelope()}}

        attempt = self._run_read_only_phase(row.task, skill="architecture-design", reply=reply, host=SimpleNamespace())

        directive.refresh_from_db()
        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert "never retry a hold" in self._turn_text()
        assert directive.state == Directive.State.INTERPRETED

    def test_scoping_runs_on_codex_and_completes_on_its_summary(self) -> None:
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, overlay="example-overlay")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="scoping")
        host = SimpleNamespace(
            get_issue=lambda url: {"body": "Scope the executor."}, list_issue_comments=lambda *, issue_url: []
        )

        attempt = self._run_read_only_phase(task, skill="ticket", reply={"summary": "scoped"}, host=host)

        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert "Scope the executor." in self._turn_text()
