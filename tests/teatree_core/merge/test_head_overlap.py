"""Pairwise overlap of one repo's open PR heads, driven over real git objects."""

import tempfile
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.merge.carrier import record_carry_freeze
from teatree.core.merge.head_overlap import (
    OpenHead,
    OpenHeadsUnreadableError,
    OverlapVerdict,
    overlap_refusal,
    pair_overlaps,
    read_open_heads,
)
from teatree.core.models import PullRequest, Ticket
from teatree.utils.pr_ref import PrRef
from tests.teatree_core.merge.overlap_helpers import (
    NUMBERS,
    RecordingForge,
    commit_on_branch,
    forge_for,
    git,
    make_three_heads,
)

_SLUG = "acme/widget"


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

    def test_the_older_pr_merges(self) -> None:
        repo, shas = self.repo, self.shas
        assert overlap_refusal(ref=self._ref(11), ticket=None, host=forge_for(shas), repo_dir=str(repo)) is None

    def test_a_pr_overlapping_nothing_merges(self) -> None:
        repo, shas = self.repo, self.shas
        assert overlap_refusal(ref=self._ref(13), ticket=None, host=forge_for(shas), repo_dir=str(repo)) is None

    def test_an_unreadable_head_list_refuses(self) -> None:
        repo = self.repo
        host = RecordingForge([], unreadable=True)

        refusal = overlap_refusal(ref=self._ref(11), ticket=None, host=host, repo_dir=str(repo))

        assert refusal is not None
        assert "could not be read" in refusal

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
