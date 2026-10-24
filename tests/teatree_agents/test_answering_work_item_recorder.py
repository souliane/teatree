"""The answering phase's ``work_item`` channel reaches the forge and the owner (#4527).

Two halves of one promise. The channel FILES — a shell-denied answerer cannot,
so the recorder does it server-side. And the reply the owner reads names the
filed issue as a clickable link, because "tracking as ticket N" for an N nobody
can open is the trap this whole change removes.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.core.answering.work_item_filing import WorkItemFilingError
from teatree.core.models import Session, Task, Ticket
from teatree.core.overlay_metadata import OverlayMetadata
from teatree.core.send_proxy import route_forge_write
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError
from teatree.utils.url_slug import slack_conversation_anchor
from tests._send_gate import allow_forge_repos

pytestmark = pytest.mark.usefixtures("configured_banned_term_registry")

_CHANNEL = "D-owner"
_REPO_A = "souliane/teatree"
_REPO_B = "souliane/docs-site"
_FILED = f"https://github.com/{_REPO_A}/issues/7100"


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
    searched: list[str] = field(default_factory=list)
    refusal: Exception | None = None

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> RawAPIDict:
        if self.refusal is not None:
            raise self.refusal
        self.created.append({"repo": repo, "title": title, "body": body, "labels": list(labels or [])})
        return {"html_url": f"https://github.com/{repo}/issues/7100", "body": body}

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        _ = query
        self.searched.append(repo)
        return []


def _answering_task(*, implies_work: bool = True, work_issue_url: str = "") -> Task:
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
                **({"work_issue_url": work_issue_url} if work_issue_url else {}),
            }
        },
    )
    session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
    return Task.objects.create(ticket=ticket, session=session, phase="answering", subject="answer the owner")


class TestTheWorkItemChannelFilesAndTellsTheOwner(TestCase):
    """A work-implying request ends as a real issue the owner can click."""

    def setUp(self) -> None:
        super().setUp()
        allow_forge_repos("souliane/teatree")
        self.backend = RecordingBackend()
        self.host = RecordingHost()
        patches = (
            mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=self.backend),
            mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host),
            mock.patch("teatree.core.answering.work_item_filing.filing_repo", return_value=_REPO_A),
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

    def test_the_filed_issue_carries_the_maintainer_gate(self) -> None:
        """The gate that makes the fail-open work-implying reader safe.

        ``IssueImplementerScanner`` skips ``needs-triage``; without it the factory claims an
        issue whose text an agent wrote from a message nobody confirmed was a request.
        """
        self._record(
            {
                "answer": {"text": "On it."},
                "work_item": {"title": "Detect the open-PR bottleneck", "body": "Alert when open PRs stall."},
            }
        )

        assert "needs-triage" in self.host.created[0]["labels"], (
            f"the recorder filed a claimable issue with no maintainer gate: {self.host.created[0]['labels']}"
        )

    def test_a_declared_no_work_reply_files_nothing_and_promises_nothing(self) -> None:
        self._record({"answer": {"text": "Already covered."}, "work_item": {"no_work_reason": "nothing to build"}})

        assert self.host.created == []
        assert _FILED not in self.backend.replies[0][2]

    def test_a_failed_filing_is_stated_in_the_thread_never_swallowed(self) -> None:
        with mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=None):
            self._record({"answer": {"text": "On it."}, "work_item": {"title": "t", "body": "b"}})

        body = self.backend.replies[0][2]
        assert "could not file" in body.lower(), f"the filing failed and the owner was told nothing: {body!r}"

    def test_a_forge_refusal_is_stated_in_the_thread_and_never_destroys_the_answer(self) -> None:
        """A non-zero ``gh`` exit — 403, rate limit, the posting gate — must not eat the reply."""
        self.host.refusal = CommandFailedError(
            cmd=["gh"], returncode=1, stdout="", stderr="HTTP 403: refused by the posting gate"
        )

        self._record({"answer": {"text": "On it."}, "work_item": {"title": "t", "body": "b"}})

        body = self.backend.replies[0][2]
        assert "could not file" in body.lower(), f"the forge refused and the owner was told nothing: {body!r}"
        assert "403" in body, f"the stated reason does not carry what the forge said: {body!r}"

    def test_a_raising_code_host_resolver_is_stated_in_the_thread_and_never_destroys_the_answer(self) -> None:
        """Resolving the host is a filing step too — an escape there takes the whole reply with it."""
        with mock.patch(
            "teatree.core.backend_factory.code_host_from_overlay",
            side_effect=ImproperlyConfigured("no code host provider is registered for t3-teatree"),
        ):
            self._record({"answer": {"text": "On it."}, "work_item": {"title": "t", "body": "b"}})

        assert self.backend.replies, "the resolver raised and the owner's commitment was dropped in silence"
        body = self.backend.replies[0][2]
        assert "could not file" in body.lower(), f"the resolver raised and the owner was told nothing: {body!r}"
        assert "no code host provider is registered" in body, (
            f"the stated reason does not carry why the host could not be resolved: {body!r}"
        )


def _pick_by_slug(*, title: str, body: str, candidates: tuple[str, ...]) -> str:
    named = [slug for slug in candidates if slug in f"{title} {body}"]
    if len(named) != 1:
        msg = f"the item names {len(named)} of {', '.join(candidates)}; name exactly one"
        raise WorkItemFilingError(msg)
    return named[0]


class _TwoRepoMetadata(OverlayMetadata):
    """An overlay owning two repos; ``choose`` is its filing policy, ``None`` keeps the inherited default."""

    def __init__(self, choose: Callable[..., str] | None) -> None:
        self.choose = choose
        self.repos = [_REPO_A, _REPO_B]
        self.asked: list[str] = []

    def get_followup_repos(self) -> list[str]:
        return self.repos

    def choose_filing_repo(self, *, title: str, body: str, candidates: tuple[str, ...]) -> str:
        self.asked.append(title)
        if self.choose is None:
            return super().choose_filing_repo(title=title, body=body, candidates=candidates)
        return self.choose(title=title, body=body, candidates=candidates)


class TestAnOverlayChoosesTheRepoAWorkItemIsFiledIn(TestCase):
    """The overlay's metadata hook picks among its owned repos from the item's own text (#5508)."""

    def setUp(self) -> None:
        super().setUp()
        allow_forge_repos(_REPO_A, _REPO_B)
        self.backend = RecordingBackend()
        self.host = RecordingHost()
        self.metadata = _TwoRepoMetadata(_pick_by_slug)
        overlay = SimpleNamespace(
            config=SimpleNamespace(code_host="github"),
            review=SimpleNamespace(merge_candidate_repo_slugs=list),
            metadata=self.metadata,
        )
        patches = (
            mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=self.backend),
            mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=self.host),
            mock.patch("teatree.core.answering.work_item_filing.get_overlay", return_value=overlay),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _record(self, work_item: dict, *, work_issue_url: str = "") -> str:
        task = _answering_task(work_issue_url=work_issue_url)
        record_reactive_envelopes(task, {"answer": {"text": "On it."}, "work_item": work_item}, phase="answering")
        return self.backend.replies[0][2]

    def test_an_item_naming_the_second_repo_is_filed_there(self) -> None:
        reply = self._record({"title": f"Document the flow in {_REPO_B}", "body": "Write it up."})

        filed = f"https://github.com/{_REPO_B}/issues/7100"
        assert [call["repo"] for call in self.host.created] == [_REPO_B]
        assert Ticket.objects.get(issue_url=filed).is_admissible()
        assert f"<{filed}|#7100>" in reply

    def test_the_dedupe_read_the_gated_writes_and_the_create_all_use_the_chosen_repo(self) -> None:
        with mock.patch("teatree.core.answering.work_item_filing.route_forge_write", wraps=route_forge_write) as routed:
            self._record({"title": f"Document the flow in {_REPO_B}", "body": "Write it up."})

        assert self.host.searched == [_REPO_B]
        assert [call.kwargs["repo"] for call in routed.call_args_list] == [_REPO_B, _REPO_B]
        assert [call["repo"] for call in self.host.created] == [_REPO_B]

    def test_an_item_naming_both_repos_is_refused_naming_the_candidates(self) -> None:
        reply = self._record({"title": f"Sync {_REPO_A} with {_REPO_B}", "body": "Keep them aligned."})

        assert "Could not file a tracking issue for this" in reply
        assert _REPO_A in reply, reply
        assert _REPO_B in reply, reply
        assert self.host.created == []
        assert self.host.searched == []

    def test_an_overlay_without_the_override_files_in_its_first_repo(self) -> None:
        self.metadata.choose = None

        self._record({"title": f"Document the flow in {_REPO_B}", "body": "Write it up."})

        assert [call["repo"] for call in self.host.created] == [_REPO_A]

    def _assert_hook_stays_quiet(self, work_item: dict, *, work_issue_url: str = "") -> None:
        reply = self._record(work_item, work_issue_url=work_issue_url)

        assert self.metadata.asked == []
        assert "Could not file" not in reply, reply

    def test_the_hook_is_not_asked_when_the_item_names_an_existing_issue(self) -> None:
        self._assert_hook_stays_quiet({"existing_issue_url": f"https://github.com/{_REPO_A}/issues/4526"})

    def test_the_hook_is_not_asked_when_the_item_declares_no_work(self) -> None:
        self._assert_hook_stays_quiet({"no_work_reason": "answered from recorded state"})

    def test_the_hook_is_not_asked_when_the_request_is_already_tracked(self) -> None:
        self._assert_hook_stays_quiet(
            {"title": f"Sync {_REPO_A} with {_REPO_B}", "body": "b"},
            work_issue_url=f"https://github.com/{_REPO_A}/issues/4242",
        )

    def test_a_hook_naming_an_undeclared_repo_is_refused(self) -> None:
        self.metadata.choose = lambda **_: "someone/else"

        reply = self._record({"title": "Anything", "body": "b"})

        assert "Could not file a tracking issue for this" in reply
        assert "someone/else" in reply, reply
        assert _REPO_A in reply, reply
        assert _REPO_B in reply, reply
        assert self.host.created == []

    def test_a_hook_that_crashes_is_stated_in_the_thread_and_never_destroys_the_answer(self) -> None:
        def crash(**_: object) -> str:
            missing = "repo-map"
            raise KeyError(missing)

        self.metadata.choose = crash

        reply = self._record({"title": "Anything", "body": "b"})

        assert "Could not file a tracking issue for this" in reply
        assert "repo-map" in reply, reply
        assert self.host.created == []

    def test_an_overlay_owning_no_repo_states_it_only_when_a_new_issue_is_due(self) -> None:
        self.metadata.repos = []

        reply = self._record({"title": "Anything", "body": "b"})

        assert "Could not file a tracking issue for this" in reply
        assert "declares no repos" in reply, reply
        assert self.host.created == []

    def test_an_overlay_owning_no_repo_still_accepts_a_declared_no_work_reply(self) -> None:
        self.metadata.repos = []

        self._assert_hook_stays_quiet({"no_work_reason": "answered from recorded state"})
