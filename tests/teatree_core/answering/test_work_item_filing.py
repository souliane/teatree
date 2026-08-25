"""An owner request becomes a REAL forge issue, or it becomes a stated refusal (#4527).

The failure this module pins: the answering phase drafted a reply, the reply
promised "tracking as ticket N", and N was a row with no ``issue_url`` that
intake — which discovers work from the forge — could never see. Fifty such rows
accumulated, each the only surviving record of one owner request.

So the filer's contract is: file (or attach to) something intake CAN find, dedupe
so a retry never forks a second issue, route every outbound byte through the
public-repo leak gate, and fail LOUD rather than return a quiet nothing that a
caller reads as "there was nothing to file".
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.answering.work_item_filing import WorkItemFilingError, file_work_item
from teatree.core.models import Ticket
from teatree.core.send_proxy import OutboundBlockedError
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_REPO = "souliane/teatree"
_Refused = OutboundBlockedError("banned term in outbound text")
_ANCHOR = "https://github.com/souliane/teatree/issues/3009#slack=D-owner/1.0/dm"


@dataclass
class RecordingHost:
    """In-memory ``CodeHostBackend`` recording every issue it is asked to file."""

    created: list[dict] = field(default_factory=list)
    open_issues: list[RawAPIDict] = field(default_factory=list)
    next_number: int = 7000

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> RawAPIDict:
        self.created.append({"repo": repo, "title": title, "body": body, "labels": list(labels or [])})
        url = f"https://github.com/{repo}/issues/{self.next_number}"
        self.next_number += 1
        raw: RawAPIDict = {"html_url": url, "body": body}
        self.open_issues.append(raw)
        return raw

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        _ = repo
        return [raw for raw in self.open_issues if query in str(raw.get("body") or "")]


def _conversation_ticket(**extra_slack: object) -> Ticket:
    slack: dict = {
        "channel": "D-owner",
        "slack_ts": "1.0",
        "question": "the open-PR bottleneck must never recur",
        "fingerprint": "fp-bottleneck",
        "intent": "instruction",
        "work_summary": "detect the open-PR bottleneck and prevent recurrence",
    }
    slack.update(extra_slack)
    return Ticket.objects.create(
        issue_url=_ANCHOR,
        overlay="t3-teatree",
        short_description="detect the open-PR bottleneck and prevent recurrence",
        extra={"slack_answer": slack},
    )


def _blocked_write(*, forge: str, repo: str, text: str, action: str, target: str) -> str:
    _ = (forge, repo, text, action, target)
    raise _Refused


class TestANewRequestBecomesAnAdmissibleIssue:
    """The primary path — the owner's request lands somewhere intake can find it."""

    def test_the_issue_is_filed_and_its_ticket_is_admissible(self) -> None:
        host = RecordingHost()

        filed = file_work_item(
            _conversation_ticket(),
            {"title": "Detect the open-PR bottleneck", "body": "Alert when open PRs stall."},
            host=host,
            repo=_REPO,
        )

        assert filed is not None
        assert filed.url.startswith(f"https://github.com/{_REPO}/issues/")
        work = Ticket.objects.get(issue_url=filed.url)
        assert work.is_admissible(), "the filed row is still not something intake could discover"
        assert work.short_description == "Detect the open-PR bottleneck"

    def test_an_owner_directed_filing_is_not_withheld_behind_needs_triage(self) -> None:
        """The owner asked for this by name, so it is admitted, not parked for triage."""
        host = RecordingHost()

        file_work_item(
            _conversation_ticket(),
            {"title": "Detect the open-PR bottleneck", "body": "Alert when open PRs stall."},
            host=host,
            repo=_REPO,
        )

        assert host.created[0]["labels"] == ["t3-auto"]

    def test_the_agent_generated_fallback_is_withheld_behind_needs_triage(self) -> None:
        """Nobody dictated this text, so the maintainer clears it before the factory claims it."""
        host = RecordingHost()

        file_work_item(
            _conversation_ticket(),
            {"title": "Detect the open-PR bottleneck", "body": "Alert when open PRs stall."},
            host=host,
            repo=_REPO,
            auto_filed=True,
        )

        assert "needs-triage" in host.created[0]["labels"]

    def test_the_conversation_row_records_where_the_work_went(self) -> None:
        ticket = _conversation_ticket()

        filed = file_work_item(
            ticket,
            {"title": "Detect the open-PR bottleneck", "body": "Alert when open PRs stall."},
            host=RecordingHost(),
            repo=_REPO,
        )

        assert filed is not None
        ticket.refresh_from_db()
        assert ticket.extra["slack_answer"]["work_issue_url"] == filed.url


class TestARetryNeverForksASecondIssue:
    """Idempotency — a re-run of a recorded run must attach, never re-file."""

    def test_the_recorded_url_short_circuits_before_any_forge_read(self) -> None:
        ticket = _conversation_ticket(work_issue_url="https://github.com/souliane/teatree/issues/4242")
        host = RecordingHost()

        filed = file_work_item(ticket, {"title": "t", "body": "b"}, host=host, repo=_REPO)

        assert filed is not None
        assert filed.url == "https://github.com/souliane/teatree/issues/4242"
        assert filed.already_filed
        assert host.created == [], "a second issue was filed for a request already tracked"

    def test_the_forge_fingerprint_marker_dedupes_an_unrecorded_refile(self) -> None:
        """A crash between the forge write and the DB stamp must not fork a second issue."""
        host = RecordingHost()
        ticket = _conversation_ticket()
        first = file_work_item(ticket, {"title": "t", "body": "b"}, host=host, repo=_REPO)
        ticket.refresh_from_db()
        ticket.extra["slack_answer"].pop("work_issue_url")
        ticket.save(update_fields=["extra"])

        second = file_work_item(ticket, {"title": "t", "body": "b"}, host=host, repo=_REPO)

        assert first is not None
        assert second is not None
        assert second.url == first.url
        assert second.already_filed
        assert len(host.created) == 1, "the fingerprint marker did not dedupe a re-file"


class TestAttachingToAnExistingBacklogIssue:
    """Reuse-before-file — the agent may name a host issue instead of minting a near-duplicate."""

    def test_an_existing_issue_is_attached_not_refiled(self) -> None:
        host = RecordingHost()

        filed = file_work_item(
            _conversation_ticket(),
            {"existing_issue_url": "https://github.com/souliane/teatree/issues/4526"},
            host=host,
            repo=_REPO,
        )

        assert filed is not None
        assert filed.url == "https://github.com/souliane/teatree/issues/4526"
        assert host.created == []
        assert Ticket.objects.filter(issue_url=filed.url).exists()

    def test_a_non_forge_reference_is_refused_rather_than_recorded(self) -> None:
        """A bare number or a made-up ref would record a promise nothing can resolve."""
        with pytest.raises(WorkItemFilingError):
            file_work_item(_conversation_ticket(), {"existing_issue_url": "4526"}, host=RecordingHost(), repo=_REPO)


class TestNothingLeaksAndNothingFailsQuietly:
    """The safety half — the leak gate holds, and a failure is never a silent empty."""

    def test_a_refused_body_withholds_the_issue_instead_of_filing_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.answering.work_item_filing.route_forge_write", _blocked_write)
        host = RecordingHost()

        filed = file_work_item(_conversation_ticket(), {"title": "t", "body": "b"}, host=host, repo=_REPO)

        assert filed is not None
        assert filed.withheld
        assert filed.url == ""
        assert filed.withheld_reason
        assert host.created == [], "a body the leak gate refused was filed anyway"

    def test_a_declared_non_work_reply_files_nothing_and_says_so(self) -> None:
        host = RecordingHost()

        filed = file_work_item(
            _conversation_ticket(),
            {"no_work_reason": "answered from recorded state; nothing to build"},
            host=host,
            repo=_REPO,
        )

        assert filed is None
        assert host.created == []

    def test_an_empty_envelope_raises_rather_than_returning_nothing(self) -> None:
        """A quiet ``None`` here is indistinguishable from "no work needed" — the exact silent drop."""
        with pytest.raises(WorkItemFilingError):
            file_work_item(_conversation_ticket(), {}, host=RecordingHost(), repo=_REPO)
