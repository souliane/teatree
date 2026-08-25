"""The answering phase's ``work_item`` channel reaches the forge and the owner (#4527).

Two halves of one promise. The channel FILES — a shell-denied answerer cannot,
so the recorder does it server-side. And the reply the owner reads names the
filed issue as a clickable link, because "tracking as ticket N" for an N nobody
can open is the trap this whole change removes.
"""

from dataclasses import dataclass, field
from unittest import mock

from django.test import TestCase

from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.core.models import Session, Task, Ticket
from teatree.types import RawAPIDict
from teatree.utils.url_slug import slack_conversation_anchor

_CHANNEL = "D-owner"
_FILED = "https://github.com/souliane/teatree/issues/7100"


@dataclass
class RecordingBackend:
    """Messaging backend that records the reply the owner would actually see."""

    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}


@dataclass
class RecordingHost:
    """Code host that hands back one filed issue URL and records the request."""

    created: list[dict] = field(default_factory=list)

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> RawAPIDict:
        self.created.append({"repo": repo, "title": title, "body": body, "labels": list(labels or [])})
        return {"html_url": _FILED, "body": body}

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        _ = (repo, query)
        return []


def _answering_task(*, implies_work: bool = True) -> Task:
    ticket = Ticket.objects.create(
        issue_url=slack_conversation_anchor(channel=_CHANNEL, slack_ts="1.0"),
        overlay="t3-teatree",
        role=Ticket.Role.AUTHOR,
        short_description="detect the open-PR bottleneck",
        extra={
            "slack_answer": {
                "channel": _CHANNEL,
                "slack_ts": "1.0",
                "question": "the open-PR bottleneck must never recur",
                "fingerprint": "fp-bottleneck",
                "intent": "instruction",
                "work_summary": "detect the open-PR bottleneck",
                "implies_work": implies_work,
            }
        },
    )
    session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
    return Task.objects.create(ticket=ticket, session=session, phase="answering", subject="answer the owner")


class TestTheWorkItemChannelFilesAndTellsTheOwner(TestCase):
    """A work-implying request ends as a real issue the owner can click."""

    def setUp(self) -> None:
        self.backend = RecordingBackend()
        self.host = RecordingHost()
        patches = (
            mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=self.backend),
            mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host),
            mock.patch("teatree.core.answering.work_item_filing.filing_repo", return_value="souliane/teatree"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _record(self, result: dict) -> Task:
        task = _answering_task()
        record_reactive_envelopes(task, result, phase="answering")
        return task

    def test_the_request_is_filed_as_a_real_issue(self) -> None:
        self._record(
            {
                "answer": {"text": "On it."},
                "work_item": {"title": "Detect the open-PR bottleneck", "body": "Alert when open PRs stall."},
            }
        )

        assert self.host.created, "the owner's request was answered and never filed"
        assert Ticket.objects.get(issue_url=_FILED).is_admissible()

    def test_the_reply_carries_the_clickable_filed_issue(self) -> None:
        self._record(
            {
                "answer": {"text": "On it."},
                "work_item": {"title": "Detect the open-PR bottleneck", "body": "Alert when open PRs stall."},
            }
        )

        body = self.backend.replies[0][2]
        assert _FILED in body, f"the owner cannot reach the issue that tracks the request: {body!r}"
        assert f"<{_FILED}|#7100>" in body, f"the reference is not a clickable Slack link: {body!r}"

    def test_a_declared_no_work_reply_files_nothing_and_promises_nothing(self) -> None:
        self._record({"answer": {"text": "Already covered."}, "work_item": {"no_work_reason": "nothing to build"}})

        assert self.host.created == []
        assert _FILED not in self.backend.replies[0][2]

    def test_a_failed_filing_is_stated_in_the_thread_never_swallowed(self) -> None:
        with mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=None):
            self._record({"answer": {"text": "On it."}, "work_item": {"title": "t", "body": "b"}})

        body = self.backend.replies[0][2]
        assert "could not file" in body.lower(), f"the filing failed and the owner was told nothing: {body!r}"
