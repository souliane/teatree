"""The issue-write facade: descriptions are the source of truth (#162).

Five rules, each with its inverse control:

1. dedupe before create — a fitting open ticket is EXTENDED, never duplicated
2. a requirement never becomes a comment — it becomes a dated description section
3. a sweep posts zero comments, for every purpose
4. every mutation is attributable to a sweep run when one is active
5. only the owner's / the factory bot's tickets may change

The inverse controls carry the weight. "Appends a section" is satisfied by almost
any implementation; "refuses to touch a colleague's ticket" and "never widens a
comment into an edit of someone else's body" are what a regression would break.
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.issue_hygiene import (
    MAX_APPEND_ATTEMPTS,
    CreateDecision,
    DescriptionSection,
    IssueDraft,
    IssueWriteConflictError,
    NotePurpose,
    SweepCommentRefusedError,
    _resolve_repo,
    append_description_section,
    create_or_extend,
    create_or_extend_by_marker,
    record_issue_note,
    section_marker,
)
from teatree.core.models import ConfigSetting, TicketSweepRun
from teatree.core.self_forge_identities import ExternalIssueRefusedError

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = "acme/widgets"
_ISSUE = "https://gitlab.com/acme/widgets/-/issues/7"
_OWNER = "alice.example"


@pytest.fixture(autouse=True)
def _allow_expected_issue_repo() -> None:
    ConfigSetting.objects.set_value("send_proxy_allowlist", [f"gitlab:{_REPO}"])


class _FakeHost:
    """An in-memory forge: issues keyed by URL, every write recorded."""

    def __init__(self, issues: dict[str, dict[str, Any]] | None = None) -> None:
        self.issues = issues or {}
        self.comments: list[tuple[str, str]] = []
        self.updates: list[tuple[str, str]] = []
        self.creates: list[dict[str, Any]] = []
        self.next_iid = 100
        # Lets a test stage a racing/dropped write without rebinding a bound method.
        self.on_update: Callable[[str, str], bool] | None = None

    # reads
    def get_issue(self, issue_url: str) -> dict[str, Any]:
        return dict(self.issues.get(issue_url, {"error": f"not found: {issue_url}"}))

    def list_repo_open_issues(self, *, repo: str) -> list[dict[str, Any]]:
        return [dict(issue) for issue in self.issues.values() if issue.get("state") == "opened"]

    def current_user(self) -> str:
        return _OWNER

    # writes
    def post_issue_comment(self, *, issue_url: str, body: str) -> dict[str, Any]:
        self.comments.append((issue_url, body))
        return {"id": len(self.comments)}

    def update_issue(self, *, issue_url: str, body: str) -> dict[str, Any]:
        self.updates.append((issue_url, body))
        if self.on_update is None or self.on_update(issue_url, body):
            self.issues.setdefault(issue_url, {})["description"] = body
        return {"web_url": issue_url}

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        self.creates.append({"repo": repo, "title": title, "body": body, "labels": labels})
        url = f"https://gitlab.com/{repo}/-/issues/{self.next_iid}"
        self.next_iid += 1
        self.issues[url] = _issue(url, body=body, title=title)
        return {"web_url": url, "iid": self.next_iid - 1}

    def repo_for_issue_url(self, issue_url: str) -> str:
        return _REPO

    @property
    def mutations(self) -> int:
        return len(self.comments) + len(self.updates) + len(self.creates)


class _EditsAfterFirstRead(_FakeHost):
    """Another writer lands *edit* right after our first read of the issue."""

    def __init__(self, issues: dict[str, dict[str, Any]], *, edit: str) -> None:
        super().__init__(issues)
        self._edit = edit
        self._reads = 0

    def get_issue(self, issue_url: str) -> dict[str, Any]:
        snapshot = super().get_issue(issue_url)
        self._reads += 1
        if self._reads == 1:
            self.issues[issue_url]["description"] = self._edit
        return snapshot


def _issue(url: str, *, body: str = "", title: str = "t", author: str = _OWNER, state: str = "opened") -> dict:
    return {
        "web_url": url,
        "title": title,
        "description": body,
        "state": state,
        "updated_at": "2026-09-22T00:00:00Z",
        "author": {"username": author},
    }


def _owner_host(body: str = "Original body.") -> _FakeHost:
    return _FakeHost({_ISSUE: _issue(_ISSUE, body=body)})


class TestRepoResolution(TestCase):
    def test_known_issue_url_provides_slug_when_host_returns_nothing(self) -> None:
        host = _owner_host()
        with patch.object(host, "repo_for_issue_url", return_value=""):
            assert _resolve_repo(host, _ISSUE) == _REPO


def _section(body: str, *, heading: str = "Request added") -> DescriptionSection:
    return DescriptionSection(heading=heading, body=body)


def _no_scrub() -> Any:
    """Pass outbound text through untouched — the leak gate has its own tests."""
    return patch("teatree.core.issue_hygiene.route_forge_write", side_effect=lambda **kw: kw["text"])


class _HygieneTestCase(TestCase):
    def setUp(self) -> None:
        self._scrub = _no_scrub()
        self._scrub.start()
        self.addCleanup(self._scrub.stop)


class TestRecordIssueNotePurposeRouting(_HygieneTestCase):
    """Rule 2: a requirement edits the description; only status/evidence stay comments."""

    def test_normative_purposes_append_to_the_description_and_post_no_comment(self) -> None:
        for purpose in (
            NotePurpose.REQUIREMENT,
            NotePurpose.CHANGE_REQUEST,
            NotePurpose.SCOPE_CHANGE,
            NotePurpose.DECISION,
        ):
            with self.subTest(purpose=purpose.value):
                host = _owner_host()
                outcome = record_issue_note(
                    host=host, issue_url=_ISSUE, purpose=purpose, content=f"the {purpose.value} text"
                )
                assert outcome.kind == "appended"
                assert host.comments == [], "a requirement in a comment is the bug this ticket fixes"
                assert len(host.updates) == 1
                written = host.updates[0][1]
                assert "Original body." in written, "the append must never rewrite what was there"
                assert f"the {purpose.value} text" in written

    def test_observational_purposes_stay_comments_outside_a_sweep(self) -> None:
        for purpose in (NotePurpose.STATUS, NotePurpose.EVIDENCE):
            with self.subTest(purpose=purpose.value):
                host = _owner_host()
                outcome = record_issue_note(host=host, issue_url=_ISSUE, purpose=purpose, content="a screenshot")
                assert outcome.kind == "commented"
                assert host.updates == [], "a status note is not the specification; it must not edit the body"
                assert host.comments == [(_ISSUE, "a screenshot")]

    def test_an_untyped_note_is_refused(self) -> None:
        host = _owner_host()
        with pytest.raises(ValueError, match="purpose"):
            record_issue_note(host=host, issue_url=_ISSUE, purpose="", content="something")
        assert host.mutations == 0

    def test_an_empty_note_is_refused_before_any_write(self) -> None:
        host = _owner_host()
        with pytest.raises(ValueError, match="content"):
            record_issue_note(host=host, issue_url=_ISSUE, purpose=NotePurpose.REQUIREMENT, content="   ")
        assert host.mutations == 0

    def test_a_normative_note_on_a_colleagues_ticket_is_refused_not_downgraded_to_a_comment(self) -> None:
        host = _FakeHost({_ISSUE: _issue(_ISSUE, author="someone.else")})
        with pytest.raises(ExternalIssueRefusedError):
            record_issue_note(host=host, issue_url=_ISSUE, purpose=NotePurpose.REQUIREMENT, content="do X")
        assert host.mutations == 0, "refusing must not fall back to posting a comment"

    def test_the_outbound_text_is_scrubbed_through_the_shared_forge_write_seam(self) -> None:
        """The leak gate must fire on the description append, not only on comments."""
        self._scrub.stop()
        host = _owner_host()
        with patch("teatree.core.issue_hygiene.route_forge_write", return_value="REDACTED") as scrub:
            record_issue_note(host=host, issue_url=_ISSUE, purpose=NotePurpose.REQUIREMENT, content="secret name")
        assert scrub.called
        assert "secret name" not in host.updates[0][1]
        self._scrub.start()


class TestObservationalNotesAreOurTicketsOnly(_HygieneTestCase):
    def test_a_status_or_evidence_comment_on_a_colleagues_ticket_is_refused(self) -> None:
        for purpose in (NotePurpose.STATUS, NotePurpose.EVIDENCE):
            with self.subTest(purpose=purpose.value):
                host = _FakeHost({_ISSUE: _issue(_ISSUE, author="someone.else")})
                with pytest.raises(ExternalIssueRefusedError):
                    record_issue_note(host=host, issue_url=_ISSUE, purpose=purpose, content="x")
                assert host.mutations == 0


class TestSweepPostsNoComments(_HygieneTestCase):
    """Rule 3: under a sweep run id, EVERY comment purpose is refused."""

    def test_every_purpose_is_refused_as_a_comment_during_a_sweep(self) -> None:
        for purpose in NotePurpose:
            with self.subTest(purpose=purpose.value):
                host = _owner_host()
                if purpose in {NotePurpose.STATUS, NotePurpose.EVIDENCE}:
                    with pytest.raises(SweepCommentRefusedError):
                        record_issue_note(
                            host=host, issue_url=_ISSUE, purpose=purpose, content="x", sweep_run_id="run-1"
                        )
                    assert host.mutations == 0
                else:
                    outcome = record_issue_note(
                        host=host, issue_url=_ISSUE, purpose=purpose, content="x", sweep_run_id="run-1"
                    )
                    assert outcome.kind == "appended"
                    assert host.comments == []


class TestAppendDescriptionSection(_HygieneTestCase):
    """The one compare-and-append primitive every description write goes through."""

    def test_the_section_is_dated_marked_and_appended_after_the_existing_body(self) -> None:
        host = _owner_host("Existing.")
        outcome = append_description_section(host=host, issue_url=_ISSUE, section=_section("new text"))
        written = host.updates[0][1]
        assert written.startswith("Existing.")
        assert section_marker(outcome.digest) in written
        assert "## Request added" in written

    def test_a_repeat_append_is_a_detected_no_op(self) -> None:
        host = _owner_host("Existing.")
        first = append_description_section(host=host, issue_url=_ISSUE, section=_section("new text"))
        second = append_description_section(host=host, issue_url=_ISSUE, section=_section("new text"))
        assert first.kind == "appended"
        assert second.kind == "already_present"
        assert len(host.updates) == 1, "a retry must not append the same section twice"

    def test_a_concurrent_human_edit_is_preserved_and_the_append_retries_onto_it(self) -> None:
        host = _owner_host("Original.")
        human_text = "Human correction added mid-flight."
        state = {"raced": False}

        def human_wins_the_first_race(issue_url: str, _body: str) -> bool:
            if state["raced"]:
                return True
            state["raced"] = True
            host.issues[issue_url]["description"] = f"Original.\n\n{human_text}"
            return False

        host.on_update = human_wins_the_first_race
        outcome = append_description_section(host=host, issue_url=_ISSUE, section=_section("new text"))

        assert outcome.kind == "appended"
        final = host.issues[_ISSUE]["description"]
        assert human_text in final, "the human's concurrent edit must survive"
        assert "new text" in final
        assert final.count(section_marker(outcome.digest)) == 1

    def test_an_edit_landing_between_the_read_and_the_write_is_never_overwritten(self) -> None:
        host = _EditsAfterFirstRead({_ISSUE: _issue(_ISSUE, body="Original.")}, edit="Original.\n\nConcurrent edit.")
        outcome = append_description_section(host=host, issue_url=_ISSUE, section=_section("new text"))

        final = host.issues[_ISSUE]["description"]
        assert outcome.kind == "appended"
        assert "Concurrent edit." in final
        assert "new text" in final
        assert all("Concurrent edit." in body for _url, body in host.updates)

    def test_a_description_that_never_takes_the_write_raises_rather_than_reporting_success(self) -> None:
        host = _owner_host("Original.")
        host.on_update = lambda _url, _body: False
        with pytest.raises(IssueWriteConflictError) as caught:
            append_description_section(host=host, issue_url=_ISSUE, section=_section("new text"))
        assert str(MAX_APPEND_ATTEMPTS) in str(caught.value)

    def test_a_colleagues_description_is_never_appended_to(self) -> None:
        host = _FakeHost({_ISSUE: _issue(_ISSUE, author="someone.else")})
        with pytest.raises(ExternalIssueRefusedError):
            append_description_section(host=host, issue_url=_ISSUE, section=_section("x"))
        assert host.mutations == 0

    def test_authority_is_checked_before_the_outbound_is_even_audited(self) -> None:
        """A refused write must leave no SendAudit row — it was never going to happen."""
        self._scrub.stop()
        host = _FakeHost({_ISSUE: _issue(_ISSUE, author="someone.else")})
        with (
            patch("teatree.core.issue_hygiene.route_forge_write") as scrub,
            pytest.raises(ExternalIssueRefusedError),
        ):
            append_description_section(host=host, issue_url=_ISSUE, section=_section("x"))
        scrub.assert_not_called()
        self._scrub.start()


class TestCreateOrExtend(_HygieneTestCase):
    """Rule 1: search the open backlog, extend what fits, record what did not."""

    def _landscape(self) -> _FakeHost:
        return _FakeHost(
            {
                "https://gitlab.com/acme/widgets/-/issues/1": _issue(
                    "https://gitlab.com/acme/widgets/-/issues/1", title="flaky deploy probe"
                ),
                "https://gitlab.com/acme/widgets/-/issues/2": _issue(
                    "https://gitlab.com/acme/widgets/-/issues/2", title="slow test lane"
                ),
            }
        )

    def test_a_fitting_open_ticket_is_extended_and_nothing_is_created(self) -> None:
        host = self._landscape()
        fits = "https://gitlab.com/acme/widgets/-/issues/1"
        outcome = create_or_extend(
            host=host,
            draft=IssueDraft(repo=_REPO, title="deploy probe flakes again", body="it flaked twice today"),
            decisions=[
                CreateDecision(candidate_url=fits, fits=True),
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="unrelated lane"),
            ],
        )
        assert outcome.kind == "extended_existing"
        assert outcome.issue_url == fits
        assert host.creates == [], "a fitting ticket means no new ticket"
        assert "it flaked twice today" in host.issues[fits]["description"]

    def test_all_candidates_rejected_creates_once_and_records_every_rejection(self) -> None:
        host = self._landscape()
        outcome = create_or_extend(
            host=host,
            draft=IssueDraft(repo=_REPO, title="new thing", body="body text"),
            decisions=[
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/1", reason="different probe"),
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="unrelated lane"),
            ],
        )
        assert outcome.kind == "created_new"
        assert len(host.creates) == 1
        filed = host.creates[0]["body"]
        assert "## Dedupe check" in filed
        assert "issues/1" in filed
        assert "different probe" in filed
        assert "issues/2" in filed
        assert "unrelated lane" in filed

    def test_an_empty_backlog_records_that_no_candidates_existed(self) -> None:
        host = _FakeHost({})
        outcome = create_or_extend(
            host=host, draft=IssueDraft(repo=_REPO, title="first ticket", body="body"), decisions=[]
        )
        assert outcome.kind == "created_new"
        assert "no open candidates" in host.creates[0]["body"].casefold()

    def test_an_unjudged_open_candidate_refuses_the_create(self) -> None:
        """Skipping the landscape pass is the failure mode; silence is not a rejection."""
        host = self._landscape()
        with pytest.raises(ValueError, match="unjudged"):
            create_or_extend(
                host=host,
                draft=IssueDraft(repo=_REPO, title="new thing", body="body"),
                decisions=[
                    CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/1", reason="different")
                ],
            )
        assert host.mutations == 0

    def test_a_rejection_without_a_reason_refuses_the_create(self) -> None:
        host = self._landscape()
        with pytest.raises(ValueError, match="reason"):
            create_or_extend(
                host=host,
                draft=IssueDraft(repo=_REPO, title="new thing", body="body"),
                decisions=[
                    CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/1", reason="  "),
                    CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="unrelated"),
                ],
            )
        assert host.mutations == 0

    def test_a_fitting_external_ticket_blocks_both_the_edit_and_the_duplicate(self) -> None:
        external = "https://gitlab.com/acme/widgets/-/issues/1"
        host = self._landscape()
        host.issues[external]["author"] = {"username": "someone.else"}
        outcome = create_or_extend(
            host=host,
            draft=IssueDraft(repo=_REPO, title="same thing", body="body"),
            decisions=[
                CreateDecision(candidate_url=external, fits=True),
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="unrelated"),
            ],
        )
        assert outcome.kind == "external_conflict"
        assert outcome.issue_url == external
        assert host.mutations == 0, "an external match must not be edited AND must not be duplicated"

    def test_a_closed_ticket_is_not_a_candidate_and_is_never_reopened(self) -> None:
        closed = "https://gitlab.com/acme/widgets/-/issues/3"
        host = self._landscape()
        host.issues[closed] = _issue(closed, title="flaky deploy probe", state="closed")
        outcome = create_or_extend(
            host=host,
            draft=IssueDraft(repo=_REPO, title="flaky deploy probe", body="body"),
            decisions=[
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/1", reason="a"),
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="b"),
            ],
        )
        assert outcome.kind == "created_new"
        assert closed not in [url for url, _ in host.updates]

    def test_a_landscape_that_grew_since_the_decisions_were_made_is_stale(self) -> None:
        host = self._landscape()
        decisions = [
            CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/1", reason="a"),
            CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="b"),
        ]
        new_url = "https://gitlab.com/acme/widgets/-/issues/9"
        host.issues[new_url] = _issue(new_url, title="filed a second ago")
        outcome = create_or_extend(
            host=host,
            draft=IssueDraft(repo=_REPO, title="new thing", body="body"),
            decisions=decisions,
            snapshot_urls=("https://gitlab.com/acme/widgets/-/issues/1", "https://gitlab.com/acme/widgets/-/issues/2"),
        )
        assert outcome.kind == "stale_snapshot"
        assert new_url in outcome.unjudged
        assert host.mutations == 0

    def test_the_dedupe_audit_section_is_scrubbed_before_it_reaches_the_forge(self) -> None:
        """The rejection reasons ride agent/MCP input same as the body — they must pass the same scrub."""
        self._scrub.stop()
        host = self._landscape()
        with patch("teatree.core.issue_hygiene.route_forge_write", return_value="REDACTED") as scrub:
            outcome = create_or_extend(
                host=host,
                draft=IssueDraft(repo=_REPO, title="new thing", body="body text"),
                decisions=[
                    CreateDecision(
                        candidate_url="https://gitlab.com/acme/widgets/-/issues/1", reason="secret codename"
                    ),
                    CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="unrelated"),
                ],
            )
        self._scrub.start()
        assert outcome.kind == "created_new"
        assert scrub.called
        filed = host.creates[0]["body"]
        assert "secret codename" not in filed, "an unscrubbed rejection reason must never reach the forge"
        assert "REDACTED" in filed

    def test_a_forge_create_error_is_raised_not_reported_as_created_new(self) -> None:
        """An unresolvable-project error dict must not be reported as a filed issue with no URL."""
        host = self._landscape()
        with (
            patch.object(host, "create_issue", return_value={"error": "404 Project Not Found"}),
            pytest.raises(IssueWriteConflictError, match="404 Project Not Found"),
        ):
            create_or_extend(
                host=host,
                draft=IssueDraft(repo=_REPO, title="new thing", body="body"),
                decisions=[
                    CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/1", reason="a"),
                    CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/2", reason="b"),
                ],
            )


class TestSweepCountsWhatItFiles(_HygieneTestCase):
    def test_a_newly_filed_ticket_is_recorded_against_the_sweep_run(self) -> None:
        run = TicketSweepRun.objects.begin(source="interactive")
        host = _FakeHost()

        outcome = create_or_extend(
            host=host,
            draft=IssueDraft(repo=_REPO, title="t", body="b"),
            decisions=[],
            sweep_run_id=run.run_id,
        )

        run.refresh_from_db()
        assert outcome.kind == "created_new"
        assert run.changed_urls == [outcome.issue_url]


class TestMarkerJudgedCreate(TestCase):
    """A deterministic filer still pays the landscape pass — it just judges mechanically.

    A retro filer or a dream reconciler has a stable fingerprint marker, so it needs no
    agent to read the backlog. What it must NOT get is a licence to skip the backlog:
    :func:`create_or_extend_by_marker` judges every open ticket from ONE fresh listing,
    which is also why it cannot race itself into an ``unjudged`` refusal the way a caller
    listing separately would.
    """

    def test_an_empty_marker_is_refused_before_any_write(self) -> None:
        candidate = "https://gitlab.com/acme/widgets/-/issues/9"
        host = _FakeHost({candidate: _issue(candidate, body="anything")})
        with _no_scrub(), pytest.raises(ValueError, match="marker"):
            create_or_extend_by_marker(host=host, draft=IssueDraft(repo=_REPO, title="t", body="b"), marker="  ")
        assert host.mutations == 0

    def test_a_marked_ticket_is_extended_never_duplicated(self) -> None:
        marked = "https://gitlab.com/acme/widgets/-/issues/9"
        host = _FakeHost({marked: _issue(marked, body="body\n\n<!-- fp: abc -->")})
        with _no_scrub():
            outcome = create_or_extend_by_marker(
                host=host, draft=IssueDraft(repo=_REPO, title="t", body="the request"), marker="<!-- fp: abc -->"
            )
        assert outcome.kind == "extended_existing"
        assert outcome.issue_url == marked
        assert host.creates == []

    def test_an_unmarked_backlog_files_once_and_records_every_rejection(self) -> None:
        other = "https://gitlab.com/acme/widgets/-/issues/9"
        host = _FakeHost({other: _issue(other, body="unrelated")})
        with _no_scrub():
            outcome = create_or_extend_by_marker(
                host=host, draft=IssueDraft(repo=_REPO, title="t", body="b"), marker="<!-- fp: abc -->"
            )
        assert outcome.kind == "created_new"
        assert len(host.creates) == 1
        assert other in host.creates[0]["body"]

    def test_an_empty_backlog_says_so_rather_than_reading_as_unchecked(self) -> None:
        host = _FakeHost()
        with _no_scrub():
            create_or_extend_by_marker(
                host=host, draft=IssueDraft(repo=_REPO, title="t", body="b"), marker="<!-- fp: abc -->"
            )
        assert "No open candidates" in host.creates[0]["body"]

    def test_a_closed_marked_ticket_is_not_reused(self) -> None:
        closed = "https://gitlab.com/acme/widgets/-/issues/9"
        host = _FakeHost({closed: _issue(closed, body="<!-- fp: abc -->", state="closed")})
        with _no_scrub():
            outcome = create_or_extend_by_marker(
                host=host, draft=IssueDraft(repo=_REPO, title="t", body="b"), marker="<!-- fp: abc -->"
            )
        assert outcome.kind == "created_new"

    def test_a_marked_ticket_someone_else_filed_is_a_conflict_not_a_duplicate(self) -> None:
        theirs = "https://gitlab.com/acme/widgets/-/issues/9"
        host = _FakeHost({theirs: _issue(theirs, body="<!-- fp: abc -->", author="a.colleague")})
        with _no_scrub():
            outcome = create_or_extend_by_marker(
                host=host, draft=IssueDraft(repo=_REPO, title="t", body="b"), marker="<!-- fp: abc -->"
            )
        assert outcome.kind == "external_conflict"
        assert host.creates == []
        assert host.updates == []

    def test_a_second_identical_file_appends_nothing_new(self) -> None:
        marked = "https://gitlab.com/acme/widgets/-/issues/9"
        host = _FakeHost({marked: _issue(marked, body="<!-- fp: abc -->")})
        draft = IssueDraft(repo=_REPO, title="t", body="the request")
        with _no_scrub():
            create_or_extend_by_marker(host=host, draft=draft, marker="<!-- fp: abc -->")
            create_or_extend_by_marker(host=host, draft=draft, marker="<!-- fp: abc -->")
        assert len(host.updates) == 1
