"""answering, requesting_review and shipping run on the read-only executor; the worker does every outward write."""

import contextlib
import json
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.harness_dispatch as harness_dispatch_mod
import teatree.agents.runner as runner_mod
from teatree.agents import codex_app_server_options, codex_read_only_server
from teatree.agents.runner import TaskUsage, run_agent
from teatree.agents.skill_routing import clear_route_availability_cache
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import (
    DeferredQuestion,
    IncomingEvent,
    PullRequest,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    TicketSweepRun,
)
from teatree.core.models.dream_gap_ledger import dream_umbrella_url
from teatree.types import SkillMetadata
from teatree.utils import run, secrets
from tests._owner_channel import assert_a_plain_card
from tests.teatree_agents._executor_fakes import FakeExecutor

_PR_URL = "https://github.com/example-org/example-repo/pull/7"


@pytest.mark.usefixtures("pass_store")
class TestDraftPhasesOnTheReadOnlyExecutor(TestCase):
    def setUp(self) -> None:
        clear_route_availability_cache()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fake = FakeExecutor(directory, reply="{}")
        self.env = self.fake.install()
        self.enterContext(self.fake.running())
        self.enterContext(patch("teatree.agents._runner_options._main_clone_cwd", return_value=None))

    def tearDown(self) -> None:
        codex_read_only_server.reset_executor_app_servers()
        clear_route_availability_cache()

    @contextlib.contextmanager
    def _world(
        self, skill: str, issues: list[dict[str, object]] | Callable[[], list[dict[str, object]]] | None = None
    ) -> Generator[None]:
        config = AgentConfig(skill_models={skill: (AgentRouteCandidate("codex_app_server", "codex-model"),)})
        host = SimpleNamespace(
            get_issue=lambda url: {"body": "", "state": "open", "user": {"login": "owner"}},
            list_issue_comments=lambda *, issue_url: [],
            fetch_pr_message=lambda *, slug, pr_id: SimpleNamespace(title="feat(x): add y", body="## What\nAdds y."),
            list_repo_open_issues=lambda *, repo: (
                (issues() if callable(issues) else issues)
                if issues is not None
                else [
                    {
                        "html_url": f"https://github.com/{repo}/issues/{number}",
                        "title": "Related work",
                        "body": "A small change in the same module.",
                        "user": {"login": login},
                    }
                    for number, login in ((1, "owner"), (2, "another-author"))
                ]
            ),
            current_user=lambda: "owner",
            get_repo=lambda *, repo: {"name": repo},
        )
        overlay = SimpleNamespace(
            metadata=SimpleNamespace(validate_pr=lambda title, body, *, repo: {"errors": []}),
            provisioning=SimpleNamespace(
                repo_clone_url=lambda _repo: "https://github.com/example-org/example-repo.git"
            ),
        )
        provider = SimpleNamespace(
            get_code_host_for_url=lambda overlay, url: host,
            get_code_host_for_repo=lambda overlay, repo: host,
        )
        with (
            patch.dict("os.environ", {**self.env, "T3_CODEX_HOME": str(self.fake.root / "worker-codex-home")}),
            patch.object(secrets, "run_bounded_group", run.run_bounded_group),
            patch.object(codex_app_server_options, "container_is_the_sandbox", lambda: True),
            patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=True),
            patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"),
            patch.object(harness_dispatch_mod, "resolve_agent_config", return_value=config),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=[skill]),
            patch("teatree.agents.read_only_venue_brief.get_backend_provider", return_value=provider),
            patch("teatree.agents.read_only_venue_brief.get_overlay_for_ticket", return_value=overlay),
            patch("teatree.agents.read_only_venue_brief.owned_repo_slugs", return_value=("example-org/example-repo",)),
            patch("teatree.agents.read_only_venue_brief._fetch_target"),
            patch("teatree.agents.sweep_proposals_recorder.get_overlay_for_ticket", return_value=overlay),
            patch(
                "teatree.agents.sweep_proposals_recorder.owned_repo_slugs", return_value=("example-org/example-repo",)
            ),
            patch("teatree.agents.sweep_proposals_recorder.get_backend_provider", return_value=provider),
            patch("teatree.agents.read_only_draft_facts.get_backend_provider", return_value=provider),
            patch("teatree.agents.read_only_draft_facts.get_overlay_for_ticket", return_value=overlay),
            patch(
                "teatree.agents.read_only_draft_facts.live_head_at",
                return_value=SimpleNamespace(sha="c" * 40, unreadable=False),
            ),
            patch(
                "teatree.agents.read_only_draft_facts.live_checks_at",
                return_value=SimpleNamespace(status="green", detail="CI green"),
            ),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            yield

    def _task(self, phase: str, ticket: Ticket) -> Task:
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def _turn_text(self) -> str:
        rpc = Path(f"{self.fake.log}.rpc").read_text(encoding="utf-8").splitlines()
        turns = [json.loads(line)["params"] for line in rpc if json.loads(line).get("method") == "turn/start"]
        return json.dumps(turns)

    def _run(
        self,
        task: Task,
        skill: str,
        issues: list[dict[str, object]] | Callable[[], list[dict[str, object]]] | None = None,
    ) -> TaskAttempt:
        with self._world(skill, issues):
            return run_agent(task, phase=task.phase, overlay_skill_metadata=SkillMetadata())

    def test_answering_runs_in_the_executor_and_the_worker_files_the_reply(self) -> None:
        event = IncomingEvent.objects.create(
            source="slack", actor="U9", channel_ref="C1", body="where is the staging URL?", idempotency_key="e-1"
        )
        ticket = Ticket.objects.create(
            issue_url=f"answer://event/{event.pk}",
            role=Ticket.Role.AUTHOR,
            overlay="t3-teatree",
            extra={"answer_event_id": event.pk},
        )
        self.fake.reply = json.dumps(
            {"summary": "drafted", "answer": {"text": "It is on the staging host.", "thread_ref": ""}}
        )
        self.fake.install()

        attempt = self._run(self._task("answering", ticket), "answerer")

        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert "where is the staging URL?" in self._turn_text()
        question = DeferredQuestion.objects.get()
        assert question.is_pending
        assert "> It is on the staging host." in assert_a_plain_card(question)

    def test_requesting_review_runs_in_the_executor_and_the_worker_sends_the_request(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://github.com/example-org/example-repo/issues/3",
            role=Ticket.Role.AUTHOR,
            overlay="t3-teatree",
        )
        PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo="example-org/example-repo", iid="7")
        self.fake.reply = json.dumps(
            {"summary": "ready", "review_request": {"ready": True, "pr_url": _PR_URL, "title": "feat(x): add y"}}
        )
        self.fake.install()
        sent: list[tuple[str, ...]] = []

        def post(command: str, *args: str, stream: object) -> None:
            sent.append((command, *args))
            stream.write('{"action": "post", "permalink": "https://chat.example/p1"}\n')

        with (
            patch("teatree.agents.review_request_recorder.call_command_streamed", side_effect=post),
            patch(
                "teatree.agents.review_request_recorder.live_head_at",
                return_value=SimpleNamespace(sha="c" * 40, unreadable=False),
            ),
        ):
            attempt = self._run(self._task("requesting_review", ticket), "review-request")

        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert _PR_URL in self._turn_text()
        assert [call[:3] for call in sent] == [("review_request_post", "--mr-url", _PR_URL)]

    def test_shipping_runs_in_the_executor_and_the_worker_queues_the_ship(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://github.com/example-org/example-repo/issues/3",
            role=Ticket.Role.AUTHOR,
            overlay="t3-teatree",
            state=Ticket.State.SELF_REVIEWED,
        )
        self.fake.reply = json.dumps({"summary": "ready", "ship_readiness": {"ready": True}})
        self.fake.install()

        def pr_create(*_args: object, **_kwargs: object) -> dict[str, object]:
            Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.PR_OPENED)
            return {"queued": True}

        with patch("teatree.agents.ship_readiness_recorder.call_command_streamed", side_effect=pr_create) as ship:
            attempt = self._run(self._task("shipping", ticket), "ship")

        assert attempt.error == "", attempt.error
        assert (attempt.exit_code, attempt.selected_harness) == (0, "codex_app_server")
        assert "SHIP STATE, read by the worker" in self._turn_text()
        assert [call.args[:3] for call in ship.call_args_list] == [("pr", "create", str(ticket.pk))]

    def test_backlog_sweep_returns_proposals_and_the_worker_finishes_the_run(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="backlog-sweep://t3-teatree", role=Ticket.Role.AUTHOR, overlay="t3-teatree"
        )
        task = self._task("backlog_sweep", ticket)
        task.execution_reason = "ASK-GATE: return proposals"
        task.save(update_fields=["execution_reason"])
        self.fake.reply = json.dumps(
            {
                "summary": "swept",
                "sweep_proposals": {
                    "examined": [
                        "https://github.com/example-org/example-repo/issues/1",
                        "https://github.com/example-org/example-repo/issues/2",
                    ],
                    "folds": [],
                    "gaps": [],
                },
            }
        )
        self.fake.install()

        attempt = self._run(task, "sweeping-tickets")

        assert attempt.error == "", attempt.error
        assert attempt.selected_harness == "codex_app_server"
        run = TicketSweepRun.objects.get()
        assert run.finished_at is not None
        assert run.external_skipped_count == 1
        assert "OPEN ISSUE INDEX" in self._turn_text()

    def test_sweep_accepts_null_body_and_any_examined_order(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="backlog-sweep://t3-teatree", role=Ticket.Role.AUTHOR, overlay="t3-teatree"
        )
        task = self._task("backlog_sweep", ticket)
        task.execution_reason = "ASK-GATE: return proposals"
        task.save(update_fields=["execution_reason"])
        urls = [f"https://github.com/example-org/example-repo/issues/{number}" for number in (1, 2)]
        issues = [{"html_url": url, "title": "Related work", "body": None, "user": {"login": "owner"}} for url in urls]
        self.fake.reply = json.dumps(
            {"summary": "swept", "sweep_proposals": {"examined": urls[::-1], "folds": [], "gaps": []}}
        )
        self.fake.install()

        attempt = self._run(task, "sweeping-tickets", issues)

        assert attempt.error == "", attempt.error
        assert TicketSweepRun.objects.get().examined_count == 2

    def test_sweep_gap_host_is_a_listed_ticket_url(self) -> None:
        host_url = "https://github.com/example-org/example-repo/issues/1"
        Ticket.objects.create(issue_url=host_url, role=Ticket.Role.AUTHOR, overlay="t3-teatree")
        Ticket.objects.create(
            issue_url=dream_umbrella_url(),
            role=Ticket.Role.AUTHOR,
            overlay="t3-teatree",
            extra={"dream_gap_pending": [{"gap_key": "gap-a"}]},
        )
        ticket = Ticket.objects.create(
            issue_url="backlog-sweep://t3-teatree", role=Ticket.Role.AUTHOR, overlay="t3-teatree"
        )
        task = self._task("backlog_sweep", ticket)
        task.execution_reason = "ASK-GATE: return proposals\nDream promotion batch (#4776)\n- gap-a: offered work"
        task.save(update_fields=["execution_reason"])
        self.fake.reply = json.dumps(
            {
                "summary": "swept",
                "sweep_proposals": {
                    "examined": [host_url],
                    "folds": [],
                    "gaps": [{"host_url": host_url, "gap_keys": ["gap-a"]}],
                },
            }
        )
        self.fake.install()
        issues = [{"html_url": host_url, "title": "Host", "body": "Work", "user": {"login": "owner"}}]

        with patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach:
            attempt = self._run(task, "sweeping-tickets", issues)

        assert attempt.error == "", attempt.error
        assert attach.call_args.args[:3] == ("ticket", "attach-gaps", str(Ticket.objects.get(issue_url=host_url).pk))
        assert "gap host" in self._turn_text().lower()

    def test_sweep_validates_the_dispatched_window_after_the_listing_moves(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="backlog-sweep://t3-teatree", role=Ticket.Role.AUTHOR, overlay="t3-teatree"
        )
        task = self._task("backlog_sweep", ticket)
        task.execution_reason = "ASK-GATE: return proposals"
        task.save(update_fields=["execution_reason"])
        urls = [f"https://github.com/example-org/example-repo/issues/{number}" for number in (1, 2)]
        rows: list[dict[str, object]] = [
            {"html_url": url, "title": "Work", "body": "Body", "user": {"login": "owner"}} for url in urls
        ]
        calls = 0

        def listing() -> list[dict[str, object]]:
            nonlocal calls
            calls += 1
            return rows if calls == 1 else rows[1:]

        self.fake.reply = json.dumps(
            {"summary": "swept", "sweep_proposals": {"examined": urls, "folds": [], "gaps": []}}
        )
        self.fake.install()

        attempt = self._run(task, "sweeping-tickets", listing)

        assert attempt.error == "", attempt.error
        assert calls == 1
