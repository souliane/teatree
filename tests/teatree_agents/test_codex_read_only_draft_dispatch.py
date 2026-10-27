"""answering, requesting_review and shipping run on the read-only executor; the worker does every outward write."""

import contextlib
import json
import tempfile
from collections.abc import Generator
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
from teatree.core.models import DeferredQuestion, IncomingEvent, PullRequest, Session, Task, TaskAttempt, Ticket
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

    def tearDown(self) -> None:
        codex_read_only_server.reset_executor_app_servers()
        clear_route_availability_cache()

    @contextlib.contextmanager
    def _world(self, skill: str) -> Generator[None]:
        config = AgentConfig(skill_models={skill: (AgentRouteCandidate("codex_app_server", "codex-model"),)})
        host = SimpleNamespace(
            get_issue=lambda url: {"body": ""},
            list_issue_comments=lambda *, issue_url: [],
            fetch_pr_message=lambda *, slug, pr_id: SimpleNamespace(title="feat(x): add y", body="## What\nAdds y."),
        )
        overlay = SimpleNamespace(metadata=SimpleNamespace(validate_pr=lambda title, body, *, repo: {"errors": []}))
        provider = SimpleNamespace(get_code_host_for_url=lambda overlay, url: host)
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

    def _run(self, task: Task, skill: str) -> TaskAttempt:
        with self._world(skill):
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
