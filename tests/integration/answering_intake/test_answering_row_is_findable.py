"""ACCEPTANCE (#4527) — a request answered by the loop ends up where intake looks.

The whole ticket in one assertion. Drive the real path end to end: an owner DM
implies work, the reactive cycle dispatches a lane, the shell-denied answering
agent hands back its ``answer`` + ``work_item``, the recorder files, and the row
that results is one :func:`decide_issue_intake` ADMITS.

The pre-fix path produced a ``Ticket`` with a blank ``issue_url`` — nothing the
forge-scoped intake queries could ever return — so the request was announced as
tracked and then lost. That is what this asserts can no longer happen.
"""

from dataclasses import dataclass, field
from unittest import mock

import pytest

from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.core.intake.factory_admission import DEFAULT_ADMIT_LABEL, decide_issue_intake
from teatree.core.models import DmContext, PendingChatInjection, Task, Ticket
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "D-owner"
_REPO = "souliane/teatree"
_FILED = f"https://github.com/{_REPO}/issues/7200"


@dataclass
class FakeSlack:
    """Minimal messaging backend — enough for the cycle's react/post/read-back path."""

    replies: list[tuple[str, str, str]] = field(default_factory=list)
    reactions: list[str] = field(default_factory=list)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts)
        self.reactions.append(emoji)
        return {"ok": True}

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        return {"ts": ts}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = channel
        return [{"ts": thread_ts, "text": text, "bot_id": "B-bot"} for _c, _t, text in self.replies]


@dataclass
class FakeForge:
    """Code host that files the owner's request and remembers the labels it carried."""

    created: list[dict] = field(default_factory=list)

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> RawAPIDict:
        self.created.append({"repo": repo, "title": title, "body": body, "labels": list(labels or [])})
        return {"html_url": _FILED, "body": body}

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        _ = (repo, query)
        return []


def _dispatch_the_owners_request(slack: FakeSlack) -> Task:
    row = PendingChatInjection.record(
        channel=_CHANNEL,
        slack_ts="1.0",
        text="factory must auto-detect the open-PR bottleneck and fix it, so it never recurs",
        context=DmContext(user_id="U-owner"),
    )
    assert row is not None
    run_slack_answer_cycle(
        messaging_resolver=lambda _overlay: slack,
        reader=lambda _text: InboundReading(
            intent=InboundIntent.INSTRUCTION,
            answerable=False,
            work_summary="detect the open-PR bottleneck and prevent recurrence",
            source=ReadingSource.MODEL,
            rationale="an instruction",
        ),
    )
    return Task.objects.get(phase="answering")


class TestARequestTheLoopAnsweredIsAlsoARequestIntakeCanFind:
    """The end-to-end contract: answered AND admissible, never one without the other."""

    def test_the_filed_row_is_admitted_by_intake(self) -> None:
        slack = FakeSlack()
        forge = FakeForge()
        task = _dispatch_the_owners_request(slack)

        with (
            mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=slack),
            mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=forge),
            mock.patch("teatree.core.answering.work_item_filing.filing_repo", return_value=_REPO),
        ):
            record_reactive_envelopes(
                task,
                {
                    "answer": {"text": "Picked this up."},
                    "work_item": {
                        "title": "Detect the open-PR merge bottleneck",
                        "body": "Measure open PRs on a cadence and alert when merges stall.",
                    },
                },
                phase="answering",
            )

        work = Ticket.objects.get(issue_url=_FILED)
        assert work.is_admissible(), "the request is answered but still invisible to intake"
        verdict = decide_issue_intake(
            {"labels": forge.created[0]["labels"], "body": forge.created[0]["body"]},
            author_trusted=False,
            work_exists=False,
            admit_label=DEFAULT_ADMIT_LABEL,
        )
        assert verdict.acts, f"intake declined the issue the owner was promised: {verdict}"

    def test_the_owner_is_told_where_the_work_went(self) -> None:
        slack = FakeSlack()
        task = _dispatch_the_owners_request(slack)

        with (
            mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=slack),
            mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=FakeForge()),
            mock.patch("teatree.core.answering.work_item_filing.filing_repo", return_value=_REPO),
        ):
            record_reactive_envelopes(
                task,
                {"answer": {"text": "Picked this up."}, "work_item": {"title": "t", "body": "b"}},
                phase="answering",
            )

        assert any(_FILED in text for _c, _t, text in slack.replies), (
            "the owner was told the request was picked up and never told what tracks it"
        )
