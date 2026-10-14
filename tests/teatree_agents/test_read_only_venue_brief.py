"""A phase in the read-only executor gets its facts inline in the turn, under fixed caps, and never a ``t3`` command."""

import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    HANDOFF_CAP_BYTES,
    ISSUE_BODY_CAP_BYTES,
    ISSUE_COMMENTS_CAP_BYTES,
    READ_ONLY_DIRECTIVE,
    TICKET_CONTEXT_CAP_BYTES,
    _issue,
)
from teatree.agents.runner_preparation import PreparedRun
from teatree.backends.github.client import GitHubCodeHost
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
        self.diffs: object = {"src/example.py": "@@ -1 +1 @@\n-old\n+new"}

    def get_issue(self, issue_url: str) -> dict[str, object]:
        assert issue_url.startswith("https://github.com/example-org/example-repo/")
        if "/pull/" in issue_url:
            return {"error": "Not a GitHub issue URL"}
        if isinstance(self.issue, Exception):
            raise self.issue
        return self.issue

    def fetch_pr_message(self, *, slug: str, pr_id: int) -> SimpleNamespace:
        assert (slug, pr_id) == ("example-org/example-repo", 5)
        return SimpleNamespace(title="Review title", body="Review PR body")

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[dict[str, object]]:
        assert (repo, pr_iid) == ("example-org/example-repo", 5)
        if isinstance(self.comments, Exception):
            raise self.comments
        return self.comments or _COMMENTS

    def list_issue_comments(self, *, issue_url: str) -> list[dict[str, object]]:
        assert issue_url.startswith("https://github.com/example-org/example-repo/")
        if isinstance(self.comments, Exception):
            raise self.comments
        return self.comments

    def get_pr_file_diffs(self, *, repo: str, pr_iid: int) -> object:
        assert (repo, pr_iid) == ("example-org/example-repo", 5)
        return self.diffs


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
                task, preflight, phase=task.phase, handoff=handoff, credential=DispatchCredential()
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

    def _phase_bundle(self, phase: str) -> list[str]:
        selection = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=Path(self.enterContext(TemporaryDirectory())),
            phase=phase,
            overlay_skill_metadata={},
            agent_declared_skills=declared_skills_for_phase(phase),
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
        skills = self._phase_bundle("planning")
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
        with patch(
            "teatree.agents.runner_preparation.build_read_only_system_context",
            return_value="x" * (MAX_APPEND_BYTES + 1024),
        ):
            refusal = self._refusal(self._task())

        assert "read-only brief over budget: system context" in str(refusal)

    def test_an_over_budget_skill_moves_whole_into_the_task_message(self) -> None:
        self._stack_skill("s" * (MAX_APPEND_BYTES + 1024))

        prepared = self._prepare(self._task())

        assert len(self._system_context(prepared).encode()) <= MAX_APPEND_BYTES
        assert "s" * (MAX_APPEND_BYTES + 1024) in prepared.prompt

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

    def test_github_pull_url_reads_the_pr_message_and_conversation_through_the_real_host(self) -> None:
        host = GitHubCodeHost(token="example-token")
        ticket = Ticket(issue_url="https://github.com/example-org/example-repo/pull/5")
        pr_response = SimpleNamespace(
            returncode=0, stdout=json.dumps({"title": "Title", "body": "PR decision"}), stderr=""
        )
        comments_response = SimpleNamespace(
            stdout=json.dumps([[{"body": "Review comment", "user": {"login": "owner"}}]])
        )
        with (
            patch("teatree.agents.read_only_venue_brief.get_backend_provider", return_value=_provider(host)),
            patch("teatree.agents.read_only_venue_brief.get_overlay_for_ticket", return_value="example-overlay"),
            patch("teatree.backends.forge_merge_rpc.run_allowed_to_fail", return_value=pr_response) as pr_call,
            patch("teatree.backends.github.api.run_checked", return_value=comments_response) as api_call,
        ):
            body, comments = _issue(ticket)

        assert body == "PR decision"
        assert "Review comment" in comments
        assert "--json" in pr_call.call_args.args[0]
        assert "GH_TOKEN" in pr_call.call_args.kwargs["env"]
        assert "repos/example-org/example-repo/issues/5/comments?per_page=100" in api_call.call_args.args[0]

    def test_gitlab_merge_request_reads_its_message_and_notes(self) -> None:
        ticket = Ticket(issue_url="https://gitlab.example/group/team/repo/-/merge_requests/17")
        host = Mock()
        host.fetch_pr_message.return_value = SimpleNamespace(body="MR decision")
        host.list_pr_comments.return_value = [{"body": "MR note", "author": {"username": "owner"}}]

        with (
            patch("teatree.agents.read_only_venue_brief.get_backend_provider", return_value=_provider(host)),
            patch("teatree.agents.read_only_venue_brief.get_overlay_for_ticket", return_value="example-overlay"),
        ):
            body, comments = _issue(ticket)

        assert body == "MR decision"
        assert "MR note" in comments
        host.fetch_pr_message.assert_called_once_with(slug="group/team/repo", pr_id=17)
        host.list_pr_comments.assert_called_once_with(repo="group/team/repo", pr_iid=17)
        host.get_issue.assert_not_called()

    def test_unreadable_issue_comments_refuse_the_executor_run(self) -> None:
        refusal = self._refusal(self._task(), _Host(comments=CommandFailedError(["gh"], 1, "", "boom")))

        assert "the issue comments could not be read" in str(refusal)

    def test_every_inlined_part_over_its_cap_refuses_the_executor_run(self) -> None:
        handoff = Path(self.enterContext(TemporaryDirectory())) / "handoff.json"
        handoff.write_text("h" * (HANDOFF_CAP_BYTES + 1), encoding="utf-8")
        ticket = Ticket.objects.create(issue_url=f"{_ISSUE_URL}2", context="t" * (TICKET_CONTEXT_CAP_BYTES + 1))
        LandscapeArtifact.record(ticket=ticket, survey=_SURVEY, recorded_by="t3:intake")
        comments = [{"user": {"login": "example-owner"}, "body": "c" * ISSUE_COMMENTS_CAP_BYTES}]
        cases: list[tuple[str, int, _Host | None, Ticket | None, Path | None]] = [
            ("issue body", ISSUE_BODY_CAP_BYTES, _Host({"body": "x" * (ISSUE_BODY_CAP_BYTES + 1)}), None, None),
            ("issue comments", ISSUE_COMMENTS_CAP_BYTES, _Host(comments=comments), None, None),
            ("ticket context", TICKET_CONTEXT_CAP_BYTES, None, ticket, None),
            ("handoff", HANDOFF_CAP_BYTES, None, None, handoff),
        ]
        for part, cap, host, part_ticket, part_handoff in cases:
            with (
                self.subTest(part=part),
                pytest.raises(HarnessFallbackError, match=rf"over budget: {part} \d+ bytes > {cap}"),
            ):
                self._prepare(self._task(part_ticket), host=host, handoff=part_handoff)

    def test_unreadable_issue_body_refuses_the_executor_run(self) -> None:
        for issue in (CommandFailedError(["gh"], 1, "", "boom"), {"error": "Not a GitHub issue URL"}):
            with self.subTest(issue=issue):
                assert "the issue body could not be read" in str(self._refusal(self._task(), _Host(issue)))

    def test_a_ticket_without_a_survey_refuses_the_executor_run(self) -> None:
        ticket = Ticket.objects.create(issue_url=f"{_ISSUE_URL}0")

        assert "no intake landscape survey" in str(self._refusal(self._task(ticket)))

    def test_review_brief_needs_no_planning_survey(self) -> None:
        self.skills = self._phase_bundle("reviewing")
        assert "review" in self.skills, self.skills
        ticket = Ticket.objects.create(issue_url=f"{_ISSUE_URL}0")
        task = self._task(ticket)
        task.phase = "reviewing"
        task.save(update_fields=["phase"])

        prepared = self._prepare(task)

        assert "Issue body (read from the forge for you):" in prepared.prompt
        assert "PHASE: reviewing" in self._system_context(prepared)
        assert "INTAKE LANDSCAPE SURVEY" not in prepared.prompt
        assert "--- SKILL: review ---" in self._system_context(prepared)

    def test_pre_pr_self_review_can_judge_the_branch_without_a_ci_claim(self) -> None:
        self.skills = self._phase_bundle("reviewing")
        task = self._task(Ticket.objects.create(issue_url=f"{_ISSUE_URL}0"))
        task.phase = "reviewing"
        task.save(update_fields=["phase"])

        prepared = self._prepare(task)
        context = self._system_context(prepared)

        assert "Use `git rev-parse HEAD` in the mounted checkout" in context
        assert "Pre-PR self-review may return merge_safe with gh_verify_result pending" in context
        assert "otherwise set pending or failed and return hold" not in context

    def test_critic_brief_needs_no_planning_survey(self) -> None:
        self.skills = self._phase_bundle("critic_reviewing")
        ticket = Ticket.objects.create(issue_url=f"{_ISSUE_URL}0")
        task = self._task(ticket)
        task.phase = "critic_reviewing"
        task.save(update_fields=["phase"])

        prepared = self._prepare(task)

        assert "INTAKE LANDSCAPE SURVEY" not in prepared.prompt
        assert "critic_reviewing" in self._system_context(prepared)
        assert "--- SKILL: review ---" in self._system_context(prepared)

    def test_remote_review_gets_the_dispatched_head_and_full_diff_from_the_worker(self) -> None:
        head = "a" * 40
        ticket = Ticket.objects.create(
            issue_url="https://github.com/example-org/example-repo/pull/5",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": head},
        )
        task = self._task(ticket)
        task.phase = "reviewing"
        task.save(update_fields=["phase"])
        with (
            patch(
                "teatree.agents.read_only_venue_brief.live_head_at",
                return_value=SimpleNamespace(sha=head, unreadable=False),
            ),
            patch(
                "teatree.agents.read_only_venue_brief.live_checks_at",
                return_value=SimpleNamespace(status="green", detail="2 workflow runs green"),
            ),
        ):
            prepared = self._prepare(task)

        assert head in prepared.prompt
        assert "Review PR body" in prepared.prompt
        assert "decision: plan it first" in prepared.prompt
        assert "src/example.py" in prepared.prompt
        assert "+new" in prepared.prompt
        assert f"WORKER CI at dispatched head {head}: green" in prepared.prompt
        assert "2 workflow runs green" in prepared.prompt
        context = self._system_context(prepared)
        assert "Use the dispatched head in the DIFF UNDER REVIEW block" in context
        assert "Set gh_verify_result green only when WORKER CI says green" in context
        assert "git rev-parse HEAD" not in context

    def test_large_review_keeps_every_skill_and_the_full_diff_within_system_budget(self) -> None:
        head = "a" * 40
        ticket = Ticket.objects.create(
            issue_url="https://github.com/example-org/example-repo/pull/5",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": head},
        )
        task = self._task(ticket)
        task.phase = "reviewing"
        task.save(update_fields=["phase"])
        self._stack_skill("required-review-rule " * 3800)
        self.skills = ["fastapi", *self._phase_bundle("reviewing")]
        host = _Host(comments=[{"body": "comment-evidence " * 3500}])
        host.diffs = {"src/example.py": "+review-evidence " * 7000}

        with (
            patch(
                "teatree.agents.read_only_venue_brief.live_head_at",
                return_value=SimpleNamespace(sha=head, unreadable=False),
            ),
            patch(
                "teatree.agents.read_only_venue_brief.live_checks_at",
                return_value=SimpleNamespace(status="green", detail="2 runs"),
            ),
        ):
            prepared = self._prepare(task, host=host)

        context = self._system_context(prepared)
        delivered = f"{context}\n{prepared.prompt}"
        assert len(context.encode()) <= MAX_APPEND_BYTES
        assert "required-review-rule " * 3800 in delivered
        assert "--- SKILL: review ---" in delivered
        assert "+review-evidence " * 7000 in prepared.prompt
        assert "comment-evidence " * 3500 in prepared.prompt

    def test_remote_review_cannot_claim_green_when_ci_is_unreadable(self) -> None:
        head = "a" * 40
        ticket = Ticket.objects.create(
            issue_url="https://github.com/example-org/example-repo/pull/5",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": head},
        )
        task = self._task(ticket)
        task.phase = "reviewing"
        task.save(update_fields=["phase"])
        with (
            patch(
                "teatree.agents.read_only_venue_brief.live_head_at",
                return_value=SimpleNamespace(sha=head, unreadable=False),
            ),
            patch(
                "teatree.agents.read_only_venue_brief.live_checks_at",
                return_value=SimpleNamespace(status="unreadable", detail="no workflow runs"),
            ),
        ):
            prepared = self._prepare(task)

        assert f"WORKER CI at dispatched head {head}: unreadable" in prepared.prompt
        assert "otherwise set pending or failed and return hold" in self._system_context(prepared)

    def test_remote_review_refuses_a_head_that_moves_during_the_diff_read(self) -> None:
        head = "a" * 40
        ticket = Ticket.objects.create(
            issue_url="https://github.com/example-org/example-repo/pull/5",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": head},
        )
        task = self._task(ticket)
        task.phase = "reviewing"
        task.save(update_fields=["phase"])
        reads = [SimpleNamespace(sha=head, unreadable=False), SimpleNamespace(sha="b" * 40, unreadable=False)]

        with patch("teatree.agents.read_only_venue_brief.live_head_at", side_effect=reads):
            assert "head moved while its diff was read" in str(self._refusal(task))

    def test_remote_review_refuses_an_unreadable_file_patch(self) -> None:
        head = "a" * 40
        ticket = Ticket.objects.create(
            issue_url="https://github.com/example-org/example-repo/pull/5",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": head},
        )
        task = self._task(ticket)
        task.phase = "reviewing"
        task.save(update_fields=["phase"])
        host = _Host()
        host.diffs = {"assets/logo.png": None}

        with patch(
            "teatree.agents.read_only_venue_brief.live_head_at",
            return_value=SimpleNamespace(sha=head, unreadable=False),
        ):
            assert "unreadable file" in str(self._refusal(task, host))

        host.diffs = ["not a mapping"]
        with patch(
            "teatree.agents.read_only_venue_brief.live_head_at",
            return_value=SimpleNamespace(sha=head, unreadable=False),
        ):
            assert "unreadable file" in str(self._refusal(task, host))

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
