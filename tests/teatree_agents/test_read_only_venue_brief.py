"""A phase in the read-only executor gets its facts inline in the turn, under fixed caps, and never a ``t3`` command."""

import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.runner as runner_mod
from teatree.agents._runner_env import DispatchCredential
from teatree.agents.context_budget import MAX_APPEND_BYTES
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.harness_registry import HarnessCapabilities, HarnessFallbackError, HarnessFallbackKind
from teatree.agents.phase_agent_skills import declared_skills_for_phase
from teatree.agents.read_only_venue_brief import (
    ABSENT_TOOL,
    ISSUE_BODY_CAP_BYTES,
    ISSUE_COMMENTS_CAP_BYTES,
    READ_ONLY_DIRECTIVE,
)
from teatree.agents.runner_preparation import PreparedRun
from teatree.core.models import LandscapeArtifact, Session, Task, TaskAttempt, Ticket
from teatree.skill_support.index import DEFAULT_SKILLS_DIR, harness_skills_dirs, resolve_skill_md
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.utils.run import CommandFailedError

_ISSUE_URL = "https://github.com/example-org/example-repo/issues/5"
_BODY = "## Problem\nThe planner needs the body.\n"
_SURVEY = {"open_prs": [{"url": "https://forge.example/pr/7"}], "worktrees": [], "recommendations": [], "warnings": []}
_T3_INVOCATION = re.compile(r"(?<![\w/:.-])t3 ")
_WORKER_BRIEFS_AT_BASE = (
    Path(__file__).parents[1] / "fixtures" / "codex_read_only_executor" / "worker_briefs_at_base.json"
)


_COMMENTS: list[dict[str, object]] = [
    {"user": {"login": "example-owner"}, "created_at": "2026-10-01T00:00:00Z", "body": "decision: plan it first"},
    {"system": True, "author": {"username": "example-bot"}, "body": "added label example"},
    {"author": {"username": "example-dev"}, "created_at": "2026-10-02T00:00:00Z", "body": "a later note"},
]


class _Host:
    def __init__(
        self,
        issue: dict[str, object] | Exception | None = None,
        comments: list[dict[str, object]] | Exception | None = None,
    ) -> None:
        self.issue = {"body": _BODY} if issue is None else issue
        self.comments = [] if comments is None else comments

    def get_issue(self, issue_url: str) -> dict[str, object]:
        assert issue_url.startswith(_ISSUE_URL.rsplit("/", 1)[0])
        if isinstance(self.issue, Exception):
            raise self.issue
        return self.issue

    def list_issue_comments(self, *, issue_url: str) -> list[dict[str, object]]:
        assert issue_url.startswith(_ISSUE_URL.rsplit("/", 1)[0])
        if isinstance(self.comments, Exception):
            raise self.comments
        return self.comments


def _provider(host: _Host) -> SimpleNamespace:
    return SimpleNamespace(get_code_host_for_url=lambda overlay, url: host)


class TestTheReadOnlyVenueBrief(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(issue_url=_ISSUE_URL, context="decision: keep the bar\nnote: two lines")
        LandscapeArtifact.record(ticket=cls.ticket, survey=_SURVEY, recorded_by="t3:intake")

    def setUp(self) -> None:
        self.skills: list[str] = []

    def _task(self, ticket: Ticket | None = None) -> Task:
        ticket = ticket or self.ticket
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="planning")

    def _prepare(
        self,
        task: Task,
        *,
        read_only: bool = True,
        host: _Host | None = None,
        handoff: Path | None = None,
        fetch_error: Exception | None = None,
    ) -> PreparedRun:
        capabilities = HarnessCapabilities(managed_lane=True, mcp=not read_only, read_only_venue=read_only)
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=capabilities), name="codex_app_server", provider=None, model=None
        )
        preflight = runner_mod._Preflight(stage_skills=[], dispatch=dispatch, skills=self.skills)
        with (
            patch(
                "teatree.agents.read_only_venue_brief.get_backend_provider",
                return_value=_provider(host or _Host()),
            ),
            patch("teatree.agents.read_only_venue_brief.get_overlay_for_ticket", return_value="example-overlay"),
            patch("teatree.agents.read_only_venue_brief.fetch_branch", side_effect=fetch_error) as self.fetch,
        ):
            return runner_mod._prepare_run(
                task, preflight, phase="planning", handoff=handoff, credential=DispatchCredential()
            )

    @staticmethod
    def _system_context(prepared: PreparedRun) -> str:
        system_prompt = prepared.options.system_prompt
        assert isinstance(system_prompt, dict)
        return str(system_prompt.get("append"))

    def _refusal(self, task: Task, host: _Host | None = None) -> HarnessFallbackError:
        with pytest.raises(HarnessFallbackError) as refused:
            self._prepare(task, host=host)
        assert (refused.value.kind, refused.value.side_effects_started) == (HarnessFallbackKind.ACCESS, False)
        return refused.value

    def _planner_bundle(self) -> list[str]:
        selection = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=Path(self.enterContext(TemporaryDirectory())),
            phase="planning",
            overlay_skill_metadata={},
            agent_declared_skills=declared_skills_for_phase("planning"),
        )
        return selection.skills

    def _stack_skill(self, body: str) -> None:
        """A placeholder ``fastapi`` stack skill in an install root of a scratch home."""
        home = Path(self.enterContext(TemporaryDirectory()))
        skill = home / ".claude" / "skills" / "fastapi" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: fastapi\ndescription: placeholder\n---\n{body}", encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {"HOME": str(home)}))
        self.skills = ["fastapi", "rules", "architecture-design"]

    def test_the_real_planner_brief_names_no_teatree_command_and_no_skill_path(self) -> None:
        skills = self._planner_bundle()
        rules = resolve_skill_md("rules", harness_skills_dirs())
        assert rules is not None
        assert _T3_INVOCATION.search(rules.read_text(encoding="utf-8"))

        self.skills = skills
        prepared = self._prepare(self._task())
        context = self._system_context(prepared)

        assert {"rules", "architecture-design"} <= set(skills)
        assert "--- SKILL: rules ---" in context
        assert "--- SKILL: architecture-design ---" in context
        assert ABSENT_TOOL in context
        assert not _T3_INVOCATION.search(context), _T3_INVOCATION.findall(context)
        assert "mcp__teatree__" not in context
        assert [str(root) for root in harness_skills_dirs() if str(root) in context] == []
        assert json.dumps(_SURVEY, sort_keys=True) not in context
        assert json.dumps(_SURVEY, sort_keys=True) in prepared.prompt

    def test_a_stack_skill_reaches_the_executor_in_full_with_its_teatree_commands_rewritten(self) -> None:
        self._stack_skill("Before planning, run `t3 placeholder check` and follow the style rules.\n")

        context = self._system_context(self._prepare(self._task()))

        assert "--- SKILL: fastapi ---" in context
        assert f"Before planning, run {ABSENT_TOOL} and follow the style rules." in context
        assert "REQUIRED: Load /fastapi" not in context
        assert [str(root) for root in harness_skills_dirs() if str(root) in context] == []

    def test_a_brief_that_still_names_a_teatree_command_refuses_the_executor_run(self) -> None:
        self._stack_skill("Run t3 doctor check before planning.\n")

        refusal = self._refusal(self._task())

        assert "still names a teatree command" in str(refusal)

    def test_the_prior_task_result_is_a_fact_in_the_task_message_not_the_system_context(self) -> None:
        parent = self._task()
        TaskAttempt.objects.create(task=parent, result={"summary": "intake done; next run t3 teatree pr create"})
        child = Task.objects.create(ticket=self.ticket, session=parent.session, parent_task=parent, phase="planning")

        prepared = self._prepare(child)

        assert "intake done; next run t3 teatree pr create" in prepared.prompt
        assert "Prior Task Result" not in self._system_context(prepared)

    def test_an_over_budget_system_context_refuses_the_executor_run(self) -> None:
        self._stack_skill("s" * (MAX_APPEND_BYTES + 1024))

        refusal = self._refusal(self._task())

        assert "read-only brief over budget: system context" in str(refusal)

    def test_planning_brief_in_the_executor_names_no_t3_command(self) -> None:
        prepared = self._prepare(self._task())
        context = self._system_context(prepared)

        assert context.startswith("\n".join(READ_ONLY_DIRECTIVE))
        assert not _T3_INVOCATION.search(context), _T3_INVOCATION.findall(context)
        assert not _T3_INVOCATION.search(prepared.prompt), _T3_INVOCATION.findall(prepared.prompt)

    def test_brief_inlines_issue_body_context_handoff_and_survey(self) -> None:
        handoff = Path(self.enterContext(TemporaryDirectory())) / "handoff.json"
        handoff.write_text(json.dumps({"task_id": 1, "result": {"summary": "intake done"}}), encoding="utf-8")

        prepared = self._prepare(self._task(), handoff=handoff)

        assert _BODY in prepared.prompt
        assert "decision: keep the bar\nnote: two lines" in prepared.prompt
        assert '"summary": "intake done"' in prepared.prompt
        assert json.dumps(_SURVEY, sort_keys=True) in prepared.prompt
        assert str(handoff.parent) not in prepared.options.add_dirs
        assert str(handoff) not in self._system_context(prepared)

    def test_issue_comments_are_inlined_oldest_first_without_system_notes(self) -> None:
        prompt = self._prepare(self._task(), host=_Host(comments=_COMMENTS)).prompt

        owner = "--- comment by example-owner at 2026-10-01T00:00:00Z ---\ndecision: plan it first"
        later = "--- comment by example-dev at 2026-10-02T00:00:00Z ---\na later note"
        assert prompt.index(owner) < prompt.index(later)
        assert "added label example" not in prompt

    def test_unreadable_issue_comments_refuse_the_executor_run(self) -> None:
        refusal = self._refusal(self._task(), _Host(comments=CommandFailedError(["gh"], 1, "", "boom")))

        assert "the issue comments could not be read" in str(refusal)

    def test_over_budget_issue_comments_refuse_the_executor_run(self) -> None:
        body = "c" * (ISSUE_COMMENTS_CAP_BYTES + 1)

        refusal = self._refusal(self._task(), _Host(comments=[{"user": {"login": "example-owner"}, "body": body}]))

        assert "read-only brief over budget: issue comments" in str(refusal)

    def test_unreadable_issue_body_refuses_the_executor_run(self) -> None:
        for issue in (CommandFailedError(["gh"], 1, "", "boom"), {"error": "Not a GitHub issue URL"}, {"body": None}):
            with self.subTest(issue=issue):
                assert "the issue body could not be read" in str(self._refusal(self._task(), _Host(issue)))

    def test_a_ticket_without_a_survey_refuses_the_executor_run(self) -> None:
        ticket = Ticket.objects.create(issue_url=f"{_ISSUE_URL}0")

        assert "no intake landscape survey" in str(self._refusal(self._task(ticket)))

    def test_over_budget_issue_body_refuses_the_executor_run(self) -> None:
        body = "x" * (200 * 1024)

        refusal = self._refusal(self._task(), _Host({"body": body}))

        assert f"read-only brief over budget: issue body {len(body)} bytes > {ISSUE_BODY_CAP_BYTES}" in str(refusal)

    def test_a_22kb_issue_body_fits(self) -> None:
        body = "y" * (22 * 1024)

        assert body in self._prepare(self._task(), host=_Host({"body": body})).prompt

    def test_ticket_context_is_never_elided_with_a_t3_pointer(self) -> None:
        lines = [f"note {index}: {'z' * 60}" for index in range(450)]
        ticket = Ticket.objects.create(issue_url=f"{_ISSUE_URL}1", context="\n".join(lines))
        LandscapeArtifact.record(ticket=ticket, survey=_SURVEY, recorded_by="t3:intake")

        prompt = self._prepare(self._task(ticket)).prompt

        assert "\n".join(lines) in prompt
        assert "truncated" not in prompt

    def _prepare_in_checkout(self, fetch_error: Exception | None = None) -> PreparedRun:
        checkout = self.enterContext(TemporaryDirectory())
        with patch("teatree.agents.read_only_venue_brief._resolve_task_cwd", return_value=checkout):
            prepared = self._prepare(self._task(), fetch_error=fetch_error)
        self.fetch.assert_called_once_with(checkout, "main")
        return prepared

    def test_the_target_is_fetched_in_the_worker_before_the_executor_run(self) -> None:
        assert _BODY in self._prepare_in_checkout().prompt

    def test_a_failed_fetch_refuses_the_executor_run(self) -> None:
        with pytest.raises(HarnessFallbackError, match="git fetch of origin/main failed"):
            self._prepare_in_checkout(CommandFailedError(["git", "fetch"], 128, "", "unreachable"))

    def test_worker_planning_brief_keeps_its_t3_steps_and_no_executor_lines(self) -> None:
        prepared = self._prepare(self._task(), read_only=False)
        context = self._system_context(prepared)

        assert "t3 tool verify-gates" in prepared.prompt
        assert "INTAKE LANDSCAPE SURVEY" in context
        assert READ_ONLY_DIRECTIVE[0] not in context
        assert _BODY not in prepared.prompt
        self.fetch.assert_not_called()


class TestWorkerBriefsAreTheBaseBytes(TestCase):
    """Captured at 0962b16fb with no skills loaded; a deliberate worker-brief change re-captures it."""

    def test_planning_coding_and_reviewing_worker_briefs_match_the_base_byte_for_byte(self) -> None:
        golden = json.loads(_WORKER_BRIEFS_AT_BASE.read_text(encoding="utf-8"))
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, context="decision: keep the bar")
        LandscapeArtifact.record(ticket=ticket, survey={"open_prs": [], "worktrees": []}, recorded_by="t3:intake")
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities(managed_lane=True)),
            name="codex_app_server",
            provider=None,
            model=None,
        )
        for phase in ("planning", "coding", "reviewing"):
            task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
            prepared = runner_mod._prepare_run(
                task,
                runner_mod._Preflight(stage_skills=[], dispatch=dispatch, skills=[]),
                phase=phase,
                handoff=None,
                credential=DispatchCredential(),
            )

            def normalized(text: str, task: Task = task) -> str:
                return (
                    text.replace(f"Task ID: {task.pk}", "Task ID: <task>")
                    .replace(f"context show {ticket.pk}`", "context show <ticket>`")
                    .replace(f"{DEFAULT_SKILLS_DIR}/", "<skills>/")
                )

            with self.subTest(phase=phase):
                system_prompt = prepared.options.system_prompt
                assert isinstance(system_prompt, dict)
                assert normalized(str(system_prompt.get("append"))) == golden[phase]["context"]
                assert normalized(prepared.prompt) == golden[phase]["prompt"]
