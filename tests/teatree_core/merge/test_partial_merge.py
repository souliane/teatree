"""A merged ``Part of #N`` pull request that declares a remainder keeps its ticket out of ``merged``.

The reading tests use paraphrased shapes of real merged bodies. The keystone tests drive
``merge_ticket_pr`` and then the board's per-tick reconcile, with only the ``gh`` subprocess
and the issue read stubbed.
"""

import io
import json
import logging
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.cli.doctor.checks_blank_work_overlays import check_blank_work_overlays
from teatree.core.merge import merge_ticket_pr
from teatree.core.merge.partial_merge import MergedPrReading, PrLink
from teatree.core.models import MergeAudit, MergeClear, PullRequest, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.loop.scanners.board_reconcile import reconcile_board
from tests._forge_stub import CLEAN_PR_BODY, CLEAN_PR_TITLE
from tests.teatree_core.test_merge_execution import _clear, _GhStub

_ISSUE_URL = "https://github.com/souliane/teatree/issues/4242"
_GH_RUNNER = "teatree.backends.forge_merge_rpc.gh_runner"
_ISSUE_READ = "teatree.backends.github.client.GitHubCodeHost.get_issue"

_STATUS_TABLE = """Part of #4242

## Acceptance status

| Item | Status | Evidence |
| --- | --- | --- |
| 1. Route rows for the three phases | **Unmet** | Not in this change. Stays on #4242. |
| 2a. Plugin probe refusal | **Met in code, live receipt pending** | Canary check added. |
| 2b. Mount of the checkouts | **Partly met** | Refusal names the path. Not done: re-homing live rows. |
| 2d. Unreadable issue body | **Met** | Empty body for forge-less tickets. |
"""
_OPEN_LINE = "Part of #4242\n\nopen: items 1, 2h, 3 and 4 above are not delivered here; they stay on #4242.\n"
_REMAINING_AFTER = "Relates to #4242\n\n- Remaining after this PR: the retro route rows and the live measurement.\n"
_ISSUE_REMAINS_OPEN = (
    "Part of #4242 (a merge never closes an issue).\n\nThe issue remains open: step two is still to do.\n"
)
_STAYS_ON_THIS_ISSUE = "Part of #4242\n\nThe live measurement stays on #4242.\n"
_BOILERPLATE = (
    "Tidies the widget.\n\n"
    "## Update at abc1234 (live head)\n"
    "- Independent review at this head: no blocking finding. The remaining readers moved. "
    "Nit-class points are listed for follow-ups.\n"
    "- Related issue: Part of #4242 (a merge never closes an issue).\n"
)
_OTHER_ISSUE_STAYS = "Part of #4242\n\nThe second step stays on #9999.\n"
_NOTHING_OPEN = "Part of #4242\n\nUnmet: none.\n"
_SPACE_AT_THE_CAP = "Part of #4242\n\nUnmet: " + "x" * 299 + " and then more words\n"
_OPEN_CAVEAT = "Part of #4242\n\nopen: independent review still needs completion before merge.\n"
_KEEPS_ISSUE_OPEN = "Part of #4242\n\nThis change keeps the issue open: the link rules need a separate path.\n"

_ISSUE_WITH_BOXES = (
    "Intro\n\n## Acceptance\n- [x] done thing\n- [ ] route the retro rows\n- [ ] measure live\n\n"
    "## Notes\n- [ ] unrelated box\n"
)


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #1773 author gate is exercised by test_merge_execution_author_gate.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


def _ticket() -> Ticket:
    return Ticket(issue_url=_ISSUE_URL, issue_number="4242", repo_namespaced_key="souliane/teatree#4242")


@pytest.mark.parametrize(
    ("body", "unmet"),
    [
        (_STATUS_TABLE, ("1. Route rows for the three phases", "2b. Mount of the checkouts")),
        (_OPEN_LINE, ("open: items 1, 2h, 3 and 4 above are not delivered here; they stay on #4242.",)),
        (_REMAINING_AFTER, ("the retro route rows and the live measurement.",)),
        (_ISSUE_REMAINS_OPEN, ("The issue remains open: step two is still to do.",)),
        (_STAYS_ON_THIS_ISSUE, ("The live measurement stays on #4242.",)),
        (_KEEPS_ISSUE_OPEN, ("This change keeps the issue open: the link rules need a separate path.",)),
    ],
    ids=[
        "status-table",
        "open-line",
        "remaining-after",
        "issue-remains-open",
        "stays-on-this-issue",
        "keeps-issue-open",
    ],
)
def test_a_declared_remainder_is_named_from_the_body(body: str, unmet: tuple[str, ...]) -> None:
    reading = MergedPrReading.parse(body, _ticket())

    assert reading.link is PrLink.PARTIAL
    assert reading.unmet == unmet
    assert reading.leaves_remainder


@pytest.mark.parametrize(
    "body",
    [_BOILERPLATE, _OTHER_ISSUE_STAYS, _NOTHING_OPEN, _OPEN_CAVEAT],
    ids=["boilerplate", "other-issue", "unmet-none", "open-caveat"],
)
def test_a_part_of_link_with_no_declared_remainder_leaves_none(body: str) -> None:
    reading = MergedPrReading.parse(body, _ticket())

    assert reading.link is PrLink.PARTIAL
    assert not reading.leaves_remainder


def test_a_socket_sentence_does_not_claim_the_issue_stays_open() -> None:
    reading = MergedPrReading.parse(
        "Part of #4242\n\nThe lease holder does not close the socket on timeout.\n", _ticket()
    )

    assert not reading.leaves_remainder


def test_unmet_with_a_real_item_starting_with_no_is_a_remainder() -> None:
    reading = MergedPrReading.parse("Part of #4242\n\nUnmet: no coverage for the retry path", _ticket())

    assert reading.unmet == ("no coverage for the retry path",)


def test_a_completed_status_cell_and_fenced_marker_do_not_declare_remainders() -> None:
    body = (
        "Part of #4242\n\n| Item | Status |\n| --- | --- |\n"
        "| Retry path | Unmet before this PR, Met now |\n\n"
        "```markdown\nUnmet: example only\n```\n"
    )

    assert not MergedPrReading.parse(body, _ticket()).leaves_remainder


def test_a_closing_example_in_a_code_fence_does_not_override_a_partial_link() -> None:
    body = "Part of #4242\n\n```markdown\nCloses #4242\n```\n\nUnmet: finish the report"

    assert MergedPrReading.parse(body, _ticket()).unmet == ("finish the report",)


@pytest.mark.parametrize(
    ("body", "link"),
    [
        ("Closes #4242\n\nopen: the rest.\n", PrLink.CLOSING),
        ("Fixes souliane/teatree#4242. Remaining after this PR: nothing real.", PrLink.CLOSING),
        ("Part of https://github.com/souliane/teatree/issues/4242\n\nopen: step two\n", PrLink.PARTIAL),
        ("Related to souliane/teatree#4242\n\nopen: step two\n", PrLink.PARTIAL),
        ("Part of #1111\n\nopen: step two\n", PrLink.NONE),
        ("Part of other/repo#4242\n\nopen: step two\n", PrLink.NONE),
        ("Tidies the widget.\n\nopen: step two\n", PrLink.NONE),
    ],
    ids=[
        "closing-wins",
        "closing-qualified",
        "partial-url",
        "related-qualified",
        "other-issue",
        "other-repo",
        "no-link",
    ],
)
def test_the_link_is_read_for_the_tickets_own_issue_only(body: str, link: PrLink) -> None:
    assert MergedPrReading.parse(body, _ticket()).link is link


def test_a_remainder_item_cut_at_the_cap_carries_no_trailing_space() -> None:
    (item,) = MergedPrReading.parse(_SPACE_AT_THE_CAP, _ticket()).unmet

    assert item == "x" * 299


def test_unchecked_acceptance_boxes_the_body_does_not_claim_are_the_fallback_remainder() -> None:
    reading = MergedPrReading.parse(_BOILERPLATE, _ticket())

    unmet = reading.with_unclaimed_issue_boxes(
        _ISSUE_WITH_BOXES, "Part of #4242. Delivered here: route the retro rows."
    ).unmet

    assert unmet == ("measure live",)


class _BodyStub(_GhStub):
    """The keystone ``gh`` stub, answering the title/body read with the scripted body."""

    def __init__(self, body: str, *, unreadable_after_merge: bool = False) -> None:
        super().__init__()
        self.body = body
        self.unreadable_after_merge = unreadable_after_merge
        self.merged = False

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        if "title,body" in argv:
            rc = 1 if self.unreadable_after_merge and self.merged else 0
            return (rc, json.dumps({"title": CLEAN_PR_TITLE, "body": self.body}), "")
        result = super().__call__(argv)
        self.merged = self.merged or any(word.endswith("/merge") for word in argv)
        return result


def _merge(ticket: Ticket, body: str, *, pr_id: int = 859, unreadable_after_merge: bool = False) -> str:
    clear = _clear(ticket, pr_id=pr_id)
    stub = _BodyStub(body, unreadable_after_merge=unreadable_after_merge)
    with patch(_GH_RUNNER, return_value=stub):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop").ticket_state


def _ticket_in(state: str) -> Ticket:
    ticket = Ticket.objects.create(overlay="t3-teatree", issue_url=_ISSUE_URL, state=state)
    PullRequest.objects.create(
        ticket=ticket,
        overlay="t3-teatree",
        url="https://github.com/souliane/teatree/pull/859",
        repo="souliane/teatree",
        iid="859",
    )
    return ticket


def _planning_tasks(ticket: Ticket) -> list[Task]:
    return list(Task.objects.filter(ticket=ticket, phase="planning", status=Task.Status.PENDING))


class TestPartOfWithDeclaredRemainder(TestCase):
    def test_the_ticket_stays_out_of_merged_and_one_planning_task_names_the_unmet_items(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)

        state = _merge(ticket, _STATUS_TABLE)

        ticket.refresh_from_db()
        assert state == ticket.state == Ticket.State.WORK_STARTED
        (task,) = _planning_tasks(ticket)
        assert "1. Route rows for the three phases" in task.execution_reason
        assert "2b. Mount of the checkouts" in task.execution_reason
        assert MergeAudit.objects.filter(clear__ticket=ticket).count() == 1

    def test_the_boards_per_tick_reconcile_does_not_merge_the_held_ticket(self) -> None:
        for held_from in (
            Ticket.State.REVIEW_REQUESTED,
            Ticket.State.PR_OPENED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.SELF_REVIEWED,
            Ticket.State.WORK_STARTED,
            Ticket.State.NOT_STARTED,
        ):
            with self.subTest(held_from=held_from):
                Ticket.objects.filter(issue_url=_ISSUE_URL).delete()
                ticket = _ticket_in(held_from)

                _merge(ticket, _OPEN_LINE)
                reconcile_board(probe_forge=False)

                ticket.refresh_from_db()
                assert ticket.state == Ticket.State.WORK_STARTED
                assert len(_planning_tasks(ticket)) == 1

    def test_the_merged_branch_is_no_longer_bound_to_its_pull_request(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        merged_url = "https://github.com/souliane/teatree/pull/859"
        other_url = "https://github.com/souliane/teatree/pull/12"
        Ticket.objects.filter(pk=ticket.pk).update(
            extra={"pr_url_by_branch": {"5670-first-step": merged_url, "other-branch": other_url}}
        )
        ticket.refresh_from_db()

        _merge(ticket, _STATUS_TABLE)

        ticket.refresh_from_db()
        assert ticket.extra["pr_url_by_branch"] == {"other-branch": other_url}

    def test_the_doctor_accepts_a_held_ticket_whose_merged_branch_was_released(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        merged_url = "https://github.com/souliane/teatree/pull/859"
        Ticket.objects.filter(pk=ticket.pk).update(
            extra={"pr_urls": [merged_url], "pr_url_by_branch": {"5670-first-step": merged_url}}
        )
        ticket.refresh_from_db()

        _merge(ticket, _STATUS_TABLE)

        output = io.StringIO()
        with redirect_stdout(output):
            passed = check_blank_work_overlays()
        assert passed is True, output.getvalue()

    def test_the_doctor_still_flags_an_open_pull_request_with_no_branch(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        open_url = "https://github.com/souliane/teatree/pull/12"
        PullRequest.objects.create(ticket=ticket, overlay="t3-teatree", url=open_url, repo="souliane/teatree", iid="12")
        Ticket.objects.filter(pk=ticket.pk).update(extra={"pr_urls": [open_url], "pr_url_by_branch": {}})

        output = io.StringIO()
        with redirect_stdout(output):
            passed = check_blank_work_overlays()

        assert passed is False
        assert "1 unresolved PR branch row(s)" in output.getvalue()

    def test_the_doctor_flags_a_merged_pull_request_without_a_reopen_stamp(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        merged_url = "https://github.com/souliane/teatree/pull/859"
        PullRequest.objects.filter(ticket=ticket).update(state=PullRequest.State.MERGED)
        Ticket.objects.filter(pk=ticket.pk).update(extra={"pr_urls": [merged_url], "pr_url_by_branch": {}})

        output = io.StringIO()
        with redirect_stdout(output):
            passed = check_blank_work_overlays()

        assert passed is False
        assert "1 unresolved PR branch row(s)" in output.getvalue()

    def test_the_fallback_remainder_comes_from_the_issue_acceptance_boxes(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)

        with patch(_ISSUE_READ, return_value={"body": _ISSUE_WITH_BOXES}):
            state = _merge(ticket, _BOILERPLATE)

        assert state == Ticket.State.WORK_STARTED
        (task,) = _planning_tasks(ticket)
        assert "route the retro rows" in task.execution_reason

    def test_an_item_cut_at_the_cap_on_a_space_is_still_recognised_by_a_second_merge(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        _merge(ticket, _SPACE_AT_THE_CAP)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.REVIEW_REQUESTED)

        state = _merge(ticket, _SPACE_AT_THE_CAP, pr_id=860)

        assert state == Ticket.State.WORK_STARTED
        assert len(_planning_tasks(ticket)) == 1

    def test_a_second_merge_declaring_the_identical_remainder_replans_once(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        _merge(ticket, _STATUS_TABLE)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.REVIEW_REQUESTED)

        state = _merge(ticket, _STATUS_TABLE, pr_id=860)

        assert state == Ticket.State.WORK_STARTED
        assert len(_planning_tasks(ticket)) == 1


class TestMergesThatStillReachMerged(TestCase):
    def test_each_body_without_a_declared_remainder_reaches_merged(self) -> None:
        bodies = {
            "closing keyword beside a remainder": "Closes #4242\n\nopen: the rest stays on #4242.\n",
            "part of with no remainder": _BOILERPLATE,
            "no link": CLEAN_PR_BODY,
            "link to another issue": "Part of #1111\n\nopen: step two\n",
        }
        for name, body in bodies.items():
            with self.subTest(name):
                Ticket.objects.filter(issue_url=_ISSUE_URL).delete()
                ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)

                assert _merge(ticket, body) == Ticket.State.MERGED
                assert _planning_tasks(ticket) == []

    def test_a_later_closing_pull_request_merges_the_held_ticket(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        _merge(ticket, _STATUS_TABLE)

        assert _merge(ticket, "Closes #4242\n", pr_id=860) == Ticket.State.MERGED

    def test_a_later_completed_merge_supersedes_pending_planning(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        _merge(ticket, _STATUS_TABLE)
        (planning,) = _planning_tasks(ticket)

        assert _merge(ticket, "Closes #4242\n", pr_id=860) == Ticket.State.MERGED

        planning.refresh_from_db()
        assert planning.status == Task.Status.FAILED
        assert _planning_tasks(ticket) == []

    def test_a_later_part_of_pull_request_with_no_remainder_merges_the_held_ticket(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        _merge(ticket, _STATUS_TABLE)

        assert _merge(ticket, _BOILERPLATE, pr_id=860) == Ticket.State.MERGED

    def test_a_reviewer_ticket_keeps_the_merge(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)
        Ticket.objects.filter(pk=ticket.pk).update(role=Ticket.Role.REVIEWER)
        ticket.refresh_from_db()

        with self.assertNoLogs("teatree.core.merge.partial_merge", level=logging.WARNING):
            state = _merge(ticket, _STATUS_TABLE)

        assert state == Ticket.State.MERGED
        assert _planning_tasks(ticket) == []

    def test_a_hold_that_fails_still_leaves_the_merge_recorded(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)

        with (
            patch("teatree.core.models.ticket.Ticket.begin_planning", side_effect=InvalidTransitionError("refused")),
            self.assertLogs("teatree.core.merge.partial_merge", level=logging.WARNING),
        ):
            state = _merge(ticket, _STATUS_TABLE)

        ticket.refresh_from_db()
        assert state == ticket.state == Ticket.State.MERGED
        assert MergeClear.objects.get(ticket=ticket).consumed_at is not None
        assert MergeAudit.objects.filter(clear__ticket=ticket).count() == 1

    def test_an_issue_read_that_raises_keeps_the_merge_and_the_audit(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)

        with (
            patch(_ISSUE_READ, side_effect=RuntimeError("forge down")),
            self.assertLogs("teatree.core.merge.partial_merge", level=logging.ERROR),
        ):
            state = _merge(ticket, _BOILERPLATE)

        assert state == Ticket.State.MERGED
        assert MergeAudit.objects.filter(clear__ticket=ticket).count() == 1

    def test_a_ticket_with_no_edge_back_to_planning_keeps_the_merge(self) -> None:
        ticket = _ticket_in(Ticket.State.PLAN_RECORDED)

        assert _merge(ticket, _STATUS_TABLE) == Ticket.State.MERGED

    def test_an_unreadable_body_after_the_merge_keeps_the_merge_and_the_audit(self) -> None:
        ticket = _ticket_in(Ticket.State.REVIEW_REQUESTED)

        with self.assertLogs("teatree.core.merge.partial_merge", level=logging.WARNING):
            state = _merge(ticket, _STATUS_TABLE, unreadable_after_merge=True)

        assert state == Ticket.State.MERGED
        assert MergeAudit.objects.filter(clear__ticket=ticket).count() == 1
