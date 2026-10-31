"""Pairwise overlap of one repo's open PR heads over real git objects, and the carrier round that frees its members."""

import string
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge.head_overlap import (
    OpenHead,
    OpenHeadsUnreadableError,
    OverlapVerdict,
    overlap_refusal,
    pair_overlaps,
    parse_carries,
    read_open_heads,
    record_carry_freeze,
)
from teatree.core.models import Finding, PullRequest, ReviewVerdict, Ticket
from teatree.core.models.carrier_freeze import frozen_by_carrier
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.worktree.branch_currency import predict_merge_conflicts
from teatree.utils.pr_ref import PrRef
from tests.factories import planned_ticket
from tests.teatree_core.merge.overlap_helpers import (
    NUMBERS,
    RecordingForge,
    commit_on_branch,
    forge_for,
    git,
    make_three_heads,
)

_SLUG = "acme/widget"
_CARRIER_URL = "https://github.com/acme/widget/pull/30"
_MEMBER_URLS = ["https://github.com/acme/widget/pull/31", "https://github.com/acme/widget/pull/32"]


@pytest.fixture
def three_heads(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    return make_three_heads(tmp_path)


class TestPairOverlaps:
    def test_only_the_two_heads_editing_the_same_lines_are_ordered(self, three_heads) -> None:
        repo, shas = three_heads
        heads = tuple(OpenHead(pr_id=NUMBERS[key], sha=sha) for key, sha in shas.items())

        found = pair_overlaps(heads, repo_dir=str(repo))

        assert [(p.older.pr_id, p.newer.pr_id, p.verdict) for p in found] == [(11, 12, OverlapVerdict.ORDER)]
        assert found[0].conflict_paths == ("shared.txt",)
        assert found[0].shared_paths == ("shared.txt",)

    def test_involving_one_pr_compares_only_the_pairs_that_name_it(self, three_heads) -> None:
        repo, shas = three_heads
        heads = tuple(OpenHead(pr_id=NUMBERS[key], sha=sha) for key, sha in shas.items())
        missing = OpenHead(pr_id=14, sha="0123456789abcdef" * 2 + string.octdigits)

        assert pair_overlaps(heads, repo_dir=str(repo), involving=13) == ()
        assert [(p.older.pr_id, p.newer.pr_id) for p in pair_overlaps(heads, repo_dir=str(repo), involving=12)] == [
            (11, 12)
        ]
        assert {p.verdict for p in pair_overlaps((*heads, missing), repo_dir=str(repo), involving=11)} == {
            OverlapVerdict.ORDER,
            OverlapVerdict.UNREADABLE,
        }

    def test_a_single_head_has_no_pairs(self, three_heads) -> None:
        repo, shas = three_heads
        assert pair_overlaps((OpenHead(pr_id=11, sha=shas["a"]),), repo_dir=str(repo)) == ()

    def test_two_heads_on_the_same_file_without_conflict_are_only_shared(self, three_heads) -> None:
        repo, _ = three_heads
        git(repo, "checkout", "-q", "main")
        sha_d = commit_on_branch(repo, "pr-d", "shared.txt", "one\ntwo\nthree\nfour\n")
        sha_e = commit_on_branch(repo, "pr-e", "shared.txt", "zero\none\ntwo\nthree\n")

        found = pair_overlaps((OpenHead(pr_id=21, sha=sha_d), OpenHead(pr_id=22, sha=sha_e)), repo_dir=str(repo))

        assert [(p.verdict, p.conflict_paths, p.shared_paths) for p in found] == [
            (OverlapVerdict.SHARED, (), ("shared.txt",))
        ]

    def test_a_head_the_clone_does_not_hold_is_unreadable_not_clear(self, three_heads) -> None:
        repo, shas = three_heads
        missing = OpenHead(pr_id=14, sha="0123456789abcdef" * 2 + string.octdigits)
        heads = (*(OpenHead(pr_id=NUMBERS[key], sha=sha) for key, sha in shas.items()), missing)

        verdicts = {(p.older.pr_id, p.newer.pr_id): p.verdict for p in pair_overlaps(heads, repo_dir=str(repo))}

        assert [verdicts[older, 14] for older in (11, 12, 13)] == [OverlapVerdict.UNREADABLE] * 3
        assert verdicts[11, 12] is OverlapVerdict.ORDER

    def test_a_missing_head_of_one_ticket_still_folds(self, three_heads) -> None:
        repo, shas = three_heads
        heads = (
            OpenHead(pr_id=11, sha=shas["a"], ticket_id=7),
            OpenHead(pr_id=14, sha="0123456789abcdef" * 2 + string.octdigits, ticket_id=7),
        )

        assert [p.verdict for p in pair_overlaps(heads, repo_dir=str(repo))] == [OverlapVerdict.FOLD]

    def test_heads_of_one_ticket_fold(self, three_heads) -> None:
        repo, shas = three_heads
        heads = (OpenHead(pr_id=11, sha=shas["a"], ticket_id=7), OpenHead(pr_id=12, sha=shas["b"], ticket_id=7))

        assert [p.verdict for p in pair_overlaps(heads, repo_dir=str(repo))] == [OverlapVerdict.FOLD]

    def test_a_carrier_and_its_member_fold_even_when_they_conflict(self, three_heads) -> None:
        repo, shas = three_heads
        heads = (
            OpenHead(pr_id=11, sha=shas["a"], ticket_id=1, carries=frozenset({12})),
            OpenHead(pr_id=12, sha=shas["b"], ticket_id=2),
        )

        assert [p.verdict for p in pair_overlaps(heads, repo_dir=str(repo))] == [OverlapVerdict.FOLD]

    def test_a_member_conflicting_with_an_outsider_is_frozen(self, three_heads) -> None:
        repo, shas = three_heads
        heads = (
            OpenHead(pr_id=11, sha=shas["a"], ticket_id=1),
            OpenHead(pr_id=12, sha=shas["b"], ticket_id=2),
            OpenHead(pr_id=13, sha=shas["c"], ticket_id=3, carries=frozenset({12})),
        )

        verdicts = {(p.older.pr_id, p.newer.pr_id): p.verdict for p in pair_overlaps(heads, repo_dir=str(repo))}

        assert verdicts[11, 12] is OverlapVerdict.FREEZE
        assert verdicts[12, 13] is OverlapVerdict.FOLD


class TestReadOpenHeads(TestCase):
    def test_one_list_call_reads_every_head_with_its_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        PullRequest.objects.create(
            ticket=ticket, overlay="t3-teatree", url=f"https://github.com/{_SLUG}/pull/11", repo=_SLUG, iid="11"
        )
        forge = RecordingForge(
            [
                {"number": 11, "head": {"sha": "a" * 40}, "body": ""},
                {
                    "number": 12,
                    "head": {"sha": "b" * 40},
                    "body": f"Carries: https://github.com/{_SLUG}/pull/11 https://github.com/other/repo/pull/99",
                },
            ]
        )

        heads = read_open_heads(_SLUG, host=forge)

        assert forge.list_calls == [(_SLUG, "open")]
        assert [(h.pr_id, h.sha, h.ticket_id, h.carries) for h in heads] == [
            (11, "a" * 40, ticket.pk, frozenset()),
            (12, "b" * 40, None, frozenset({11})),
        ]

    def test_an_unreadable_list_raises_instead_of_reading_as_no_overlap(self) -> None:
        with pytest.raises(OpenHeadsUnreadableError, match="forge down"):
            read_open_heads(_SLUG, host=RecordingForge([], unreadable=True))

    def test_a_head_the_forge_cannot_name_raises(self) -> None:
        with pytest.raises(OpenHeadsUnreadableError, match="#12"):
            read_open_heads(_SLUG, host=RecordingForge([{"number": 12, "body": ""}]))


@pytest.mark.real_open_heads
class TestOverlapRefusal(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo, self.shas = make_three_heads(Path(self._tmp.name))

    @staticmethod
    def _ref(pr_id: int) -> PrRef:
        return PrRef(slug=_SLUG, pr_id=pr_id)

    def test_the_newer_pr_is_refused_naming_the_older(self) -> None:
        repo, shas = self.repo, self.shas

        refusal = overlap_refusal(ref=self._ref(12), ticket=None, host=forge_for(shas), repo_dir=str(repo))

        assert refusal is not None
        assert f"{_SLUG}#11" in refusal
        assert "shared.txt" in refusal

    def test_a_draft_older_pr_does_not_hold_the_newer_one_back(self) -> None:
        repo, shas = self.repo, self.shas
        host = forge_for(shas)
        host.prs[0]["draft"] = True

        assert overlap_refusal(ref=self._ref(12), ticket=None, host=host, repo_dir=str(repo)) is None

    def test_an_older_pr_on_a_review_hold_does_not_hold_the_newer_one_back(self) -> None:
        repo, shas = self.repo, self.shas
        ReviewVerdict.record(
            pr_id=11,
            slug=_SLUG,
            reviewed_sha=shas["a"],
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="failed",
            findings=[Finding(severity="blocker", summary="wrong behaviour")],
        )

        assert overlap_refusal(ref=self._ref(12), ticket=None, host=forge_for(shas), repo_dir=str(repo)) is None

    def test_a_pr_the_open_list_does_not_name_is_refused_not_passed(self) -> None:
        host = RecordingForge([])

        refusal = overlap_refusal(ref=self._ref(12), ticket=None, host=host, repo_dir=str(self.repo))

        assert refusal is not None
        assert f"does not name {_SLUG}#12" in refusal

    def test_the_older_pr_merges(self) -> None:
        repo, shas = self.repo, self.shas
        assert overlap_refusal(ref=self._ref(11), ticket=None, host=forge_for(shas), repo_dir=str(repo)) is None

    def test_a_pr_overlapping_nothing_merges(self) -> None:
        repo, shas = self.repo, self.shas
        assert overlap_refusal(ref=self._ref(13), ticket=None, host=forge_for(shas), repo_dir=str(repo)) is None

    def test_the_refusal_compares_only_the_pairs_naming_the_pr_in_git(self) -> None:
        repo, shas = self.repo, self.shas

        with patch("teatree.core.merge.head_overlap.predict_merge_conflicts", wraps=predict_merge_conflicts) as predict:
            overlap_refusal(ref=self._ref(12), ticket=None, host=forge_for(shas), repo_dir=str(repo))

        assert [call.args[1:] for call in predict.call_args_list] == [(shas["b"], shas["a"]), (shas["c"], shas["b"])]

    def test_an_unreadable_head_list_refuses(self) -> None:
        repo = self.repo
        host = RecordingForge([], unreadable=True)

        refusal = overlap_refusal(ref=self._ref(11), ticket=None, host=host, repo_dir=str(repo))

        assert refusal is not None
        assert "could not be read" in refusal

    def test_a_pr_whose_head_the_clone_cannot_read_is_refused_naming_the_older_pr(self) -> None:
        repo, shas = self.repo, self.shas
        host = forge_for(shas)
        host.prs.append({"number": 14, "head": {"sha": "0123456789abcdef" * 2 + string.octdigits}, "body": ""})

        refusal = overlap_refusal(ref=self._ref(14), ticket=None, host=host, repo_dir=str(repo))

        assert refusal is not None
        assert f"{_SLUG}#11" in refusal
        assert "could not be compared" in refusal

    def test_the_older_pr_still_merges_when_a_newer_head_is_unreadable(self) -> None:
        repo, shas = self.repo, self.shas
        host = forge_for(shas)
        host.prs.append({"number": 14, "head": {"sha": "0123456789abcdef" * 2 + string.octdigits}, "body": ""})

        assert overlap_refusal(ref=self._ref(11), ticket=None, host=host, repo_dir=str(repo)) is None

    def test_other_open_prs_and_no_local_clone_refuses(self) -> None:
        with patch("teatree.core.merge.head_overlap.matching_clone", return_value=None):
            refusal = overlap_refusal(ref=self._ref(12), ticket=None, host=forge_for(self.shas))

        assert refusal is not None
        assert "no local clone" in refusal

    def test_a_lone_open_pr_needs_no_clone(self) -> None:
        host = RecordingForge([{"number": 12, "head": {"sha": self.shas["b"]}, "body": ""}])

        with patch("teatree.core.merge.head_overlap.matching_clone", return_value=None):
            assert overlap_refusal(ref=self._ref(12), ticket=None, host=host) is None

    def test_a_member_of_an_open_carrier_is_refused_naming_the_carrier(self) -> None:
        repo, shas = self.repo, self.shas
        carrier_ticket = Ticket.objects.create(overlay="t3-teatree")
        member_ticket = Ticket.objects.create(overlay="t3-teatree")
        carrier_url = f"https://github.com/{_SLUG}/pull/13"
        member_url = f"https://github.com/{_SLUG}/pull/11"
        for ticket, url, iid in ((carrier_ticket, carrier_url, "13"), (member_ticket, member_url, "11")):
            PullRequest.objects.create(ticket=ticket, overlay="t3-teatree", url=url, repo=_SLUG, iid=iid)
        record_carry_freeze(carrier_ticket=carrier_ticket, carrier_url=carrier_url, member_urls=[member_url])
        member_ticket.refresh_from_db()

        refusal = overlap_refusal(ref=self._ref(11), ticket=member_ticket, host=forge_for(shas), repo_dir=str(repo))

        assert refusal is not None
        assert carrier_url in refusal


class TestParseCarries:
    def test_reads_the_urls_of_the_single_carries_line(self) -> None:
        body = f"Merge round for the retro fixes.\n\nCarries: {_MEMBER_URLS[0]} {_MEMBER_URLS[1]}\n\nMore text."
        assert parse_carries(body) == tuple(_MEMBER_URLS)

    def test_no_line_declares_nothing(self) -> None:
        assert parse_carries("A PR body with no round declared.") == ()

    def test_two_lines_are_ambiguous_and_declare_nothing(self) -> None:
        body = f"Carries: {_MEMBER_URLS[0]}\nCarries: {_MEMBER_URLS[1]}"
        assert parse_carries(body) == ()

    def test_punctuation_after_a_url_is_not_part_of_it(self) -> None:
        assert parse_carries(f"Carries: {_MEMBER_URLS[0]}, {_MEMBER_URLS[1]}.") == tuple(_MEMBER_URLS)


class TestFrozenByCarrier(TestCase):
    @staticmethod
    def _pull_request(ticket: Ticket, url: str) -> PullRequest:
        return PullRequest.objects.create(
            ticket=ticket, overlay=ticket.overlay, url=url, repo="acme/widget", iid=url.rsplit("/", 1)[1]
        )

    def _round(self) -> tuple[Ticket, Ticket, PullRequest]:
        carrier = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/1")
        member = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/2")
        carrier_pr = self._pull_request(carrier, _CARRIER_URL)
        self._pull_request(member, _MEMBER_URLS[0])
        record_carry_freeze(carrier_ticket=carrier, carrier_url=_CARRIER_URL, member_urls=_MEMBER_URLS)
        member.refresh_from_db()
        carrier.refresh_from_db()
        return carrier, member, carrier_pr

    def test_recording_marks_the_member_and_the_carrier(self) -> None:
        carrier, member, _ = self._round()

        assert member.extra["carried_by"] == _CARRIER_URL
        assert carrier.extra["carries"] == _MEMBER_URLS

    def test_the_member_is_frozen_and_the_carrier_never_is(self) -> None:
        carrier, member, _ = self._round()

        assert frozen_by_carrier(member) == _CARRIER_URL
        assert frozen_by_carrier(carrier) is None

    def test_minting_a_coding_task_on_a_member_raises_naming_the_carrier(self) -> None:
        _, member, _ = self._round()

        with pytest.raises(InvalidTransitionError, match=_CARRIER_URL):
            member.schedule_coding()

    def test_minting_a_debugging_task_on_a_member_raises_naming_the_carrier(self) -> None:
        _, member, _ = self._round()

        with pytest.raises(InvalidTransitionError, match=_CARRIER_URL):
            member.schedule_implementing("debugging", reason="fix the red check")

    def test_the_carrier_ticket_still_mints_coding(self) -> None:
        carrier, _, _ = self._round()

        assert carrier.schedule_coding().phase == "coding"

    def test_a_carrier_that_is_itself_carried_is_never_frozen(self) -> None:
        outer = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/1")
        inner = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/2")
        deepest = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/3")
        for ticket, url in ((outer, _CARRIER_URL), (inner, _MEMBER_URLS[0]), (deepest, _MEMBER_URLS[1])):
            self._pull_request(ticket, url)

        record_carry_freeze(carrier_ticket=outer, carrier_url=_CARRIER_URL, member_urls=[_MEMBER_URLS[0]])
        record_carry_freeze(carrier_ticket=inner, carrier_url=_MEMBER_URLS[0], member_urls=[_MEMBER_URLS[1]])
        inner.refresh_from_db()

        assert inner.extra["carried_by"] == _CARRIER_URL
        assert frozen_by_carrier(inner) is None
        assert inner.schedule_coding().phase == "coding"

    def test_a_carrier_naming_its_own_pull_request_is_not_marked_as_carried(self) -> None:
        carrier = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/1")
        member = planned_ticket(overlay="t3-teatree", issue_url="https://github.com/acme/widget/issues/2")
        self._pull_request(carrier, _CARRIER_URL)
        self._pull_request(member, _MEMBER_URLS[0])

        record_carry_freeze(
            carrier_ticket=carrier, carrier_url=_CARRIER_URL, member_urls=[_CARRIER_URL, _MEMBER_URLS[0]]
        )
        carrier.refresh_from_db()
        member.refresh_from_db()

        assert "carried_by" not in carrier.extra
        assert member.extra["carried_by"] == _CARRIER_URL

    def _assert_released_by(self, released_state: str) -> None:
        _, member, carrier_pr = self._round()
        PullRequest.objects.filter(pk=carrier_pr.pk).update(state=released_state)

        assert frozen_by_carrier(member) is None
        assert member.schedule_coding().phase == "coding"

    def test_closing_the_carrier_releases_the_member(self) -> None:
        self._assert_released_by(PullRequest.State.CLOSED)

    def test_merging_the_carrier_releases_the_member(self) -> None:
        self._assert_released_by(PullRequest.State.MERGED)
