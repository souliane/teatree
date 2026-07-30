"""The agent-session index and its transcript click-through (#3873).

``/dash/transcript/<session_id>/`` answered 200 from the day it shipped, but the only
link to it lived in a ticket's drawer, so reading a transcript meant already knowing
which ticket to open. These tests pin the nav home that fixes that, the bound on the
index, and — the constraint that matters most on this surface — that no configured
secret reaches the response bytes of either page.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.dash.sessions import SESSION_ROWS, build_session_index
from teatree.dash.views.base import NAV_ITEMS
from tests.factories import TaskFactory, TicketFactory

State = Ticket.State
_SECRET = "hunter2-supersecret-token"


class SessionIndexIsReachableTestCase(TestCase):
    def test_the_nav_offers_the_sessions_page(self) -> None:
        assert ("dash:sessions", "Sessions") in NAV_ITEMS

    def test_the_page_links_each_session_to_its_transcript(self) -> None:
        ticket = TicketFactory(state=State.STARTED, short_description="session subject")
        TaskAttempt.objects.create(
            task=TaskFactory(ticket=ticket, phase="coding"),
            execution_target="headless",
            agent_session_id="sess-abc",
            model="claude-opus-4-8",
        )
        body = self.client.get(reverse("dash:sessions")).content.decode()
        assert reverse("dash:transcript", args=["sess-abc"]) in body
        assert "session subject" in body

    def test_the_shell_keeps_its_heading_skip_link_and_labelled_nav(self) -> None:
        body = self.client.get(reverse("dash:sessions")).content.decode()
        assert "<h1" in body
        assert 'class="skip-link"' in body
        assert 'aria-label="Dashboard sections"' in body

    def test_the_transcript_page_lands_back_on_the_sessions_nav_entry(self) -> None:
        body = self.client.get(reverse("dash:transcript", args=["sess-abc"])).content.decode()
        assert reverse("dash:sessions") in body


class SessionIndexIsBoundedTestCase(TestCase):
    def test_one_row_per_session_however_many_attempts_it_produced(self) -> None:
        task = TaskFactory(ticket=TicketFactory(state=State.STARTED), phase="coding")
        for _ in range(5):
            TaskAttempt.objects.create(task=task, execution_target="headless", agent_session_id="sess-repeat")
        assert [row.agent_session_id for row in build_session_index()] == ["sess-repeat"]

    def test_the_index_never_exceeds_its_page_size(self) -> None:
        task = TaskFactory(ticket=TicketFactory(state=State.STARTED), phase="coding")
        TaskAttempt.objects.bulk_create(
            TaskAttempt(task=task, execution_target="headless", agent_session_id=f"sess-{index}")
            for index in range(SESSION_ROWS + 5)
        )
        assert len(build_session_index()) == SESSION_ROWS

    def test_an_attempt_with_no_transcript_is_not_listed(self) -> None:
        task = TaskFactory(ticket=TicketFactory(state=State.STARTED), phase="coding")
        TaskAttempt.objects.create(task=task, execution_target="headless", agent_session_id="")
        assert build_session_index() == ()


class NoConfiguredSecretReachesTheResponseTestCase(TestCase):
    """A transcript is exactly where a leaked token would surface.

    The index carries dispatch facts only — never an attempt's ``error`` body — and
    the transcript tail routes every line through the shared leak-gate redactor, so a
    term the operator banned cannot appear in either page's bytes.
    """

    def setUp(self) -> None:
        # The redactor resolves its term list Django-free (``cold_reader`` against the
        # canonical store), so a test DB row would never reach it — ``T3_BANNED_TERMS``
        # is the documented override that the same resolver reads first.
        env = patch.dict(os.environ, {"T3_BANNED_TERMS": _SECRET})
        env.start()
        self.addCleanup(env.stop)
        self.ticket = TicketFactory(state=State.STARTED)
        self.task = TaskFactory(ticket=self.ticket, phase="coding")

    def test_the_index_does_not_echo_an_attempts_error_body(self) -> None:
        TaskAttempt.objects.create(
            task=self.task,
            execution_target="headless",
            agent_session_id="sess-leak",
            error=f"auth failed with {_SECRET}",
        )
        assert _SECRET not in self.client.get(reverse("dash:sessions")).content.decode()

    def test_the_transcript_tail_redacts_the_banned_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            (projects / "-p").mkdir()
            (projects / "-p" / "sess-leak.jsonl").write_text(
                json.dumps({"type": "assistant", "message": {"content": f"exported {_SECRET} to the env"}}) + "\n",
                encoding="utf-8",
            )
            with patch("teatree.dash.transcript._projects_dir", return_value=projects):
                response = self.client.get(reverse("dash:transcript", args=["sess-leak"]))
        assert response.status_code == 200
        assert _SECRET not in response.content.decode()
