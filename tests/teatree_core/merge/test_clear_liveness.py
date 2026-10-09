"""``clear_liveness`` — a stall requires positive evidence the PR is OPEN (#4250).

The alarm this classifier replaces read "no ``MergeAudit``" as "the merge stalled" and
was 6/6 false on live data: every one of those PRs had merged outside the keystone, so
no audit was ever written. Absence of a local audit is not evidence that no merge
happened, and the tests below pin the inverted invariant — plus the fail-safe default
that stops a caller who forgets to inject a reader from paging on rows it never read.

Every reader here is a fake. No test may reach a forge: the CI test image has no ``gh``,
so an accidental live read passes locally and dies with ``FileNotFoundError`` in CI.
"""

from unittest.mock import patch

import django.test

from teatree.core.backend_protocols import PrOpenState
from teatree.core.merge.clear_liveness import ClearLiveness, classify, clear_pr_url, probe, unverified_reader
from tests.factories import MergeClearFactory

_CLONE_SLUG = "teatree.core.merge.pr_slug_resolution._project_repo_slug"


def _reads(state: str) -> object:
    def read(pr_url: str) -> str:
        return state

    return read


class ClassifyTests(django.test.TestCase):
    SLUG = "souliane/teatree"

    def _clear(self, *, pr_id: int = 4142, slug: str = SLUG) -> object:
        return MergeClearFactory(ticket=None, pr_id=pr_id, slug=slug)

    def test_a_merged_pr_is_not_a_stall(self) -> None:
        # Mirrors live CLEAR 557 / souliane/teatree#4142: unconsumed, no MergeAudit,
        # 187h old — and MERGED. The old premise called this a stalled merge.
        verdict = classify(self._clear(), read=_reads(PrOpenState.MERGED))

        assert verdict is ClearLiveness.MERGED

    def test_an_open_pr_is_a_stall(self) -> None:
        assert classify(self._clear(), read=_reads(PrOpenState.OPEN)) is ClearLiveness.STALLED

    def test_a_closed_pr_is_abandoned_not_a_stall(self) -> None:
        assert classify(self._clear(), read=_reads(PrOpenState.CLOSED)) is ClearLiveness.ABANDONED

    def test_an_unresolvable_pr_is_unverified_not_open(self) -> None:
        # Mirrors rows 618/619 (#4242/#4343): gh resolves neither, so the reader
        # collapses to UNKNOWN. A "not merged implies open" default would re-create
        # the whole defect the moment those rows cross the staleness threshold.
        assert classify(self._clear(), read=_reads(PrOpenState.UNKNOWN)) is ClearLiveness.UNVERIFIED

    def test_an_empty_state_is_unverified(self) -> None:
        assert classify(self._clear(), read=_reads("")) is ClearLiveness.UNVERIFIED

    def test_a_reader_that_raises_is_unverified_for_that_row_alone(self) -> None:
        def boom(pr_url: str) -> str:
            raise RuntimeError(pr_url)

        assert classify(self._clear(), read=boom) is ClearLiveness.UNVERIFIED

    def test_the_default_reader_can_never_page(self) -> None:
        # Fail-safe by construction: a caller that forgets to inject cannot produce a
        # STALLED verdict, whatever the forge would have said.
        assert classify(self._clear()) is ClearLiveness.UNVERIFIED
        assert unverified_reader("https://github.com/souliane/teatree/pull/1") == PrOpenState.UNKNOWN

    def test_an_unresolvable_clear_short_circuits_with_no_forge_call(self) -> None:
        # A workstream slug with no ticket and no clone origin resolves to no repo, so
        # build_pr_url returns "" and the row costs no forge read at all.
        reads: list[str] = []

        def counted(pr_url: str) -> str:
            reads.append(pr_url)
            return PrOpenState.OPEN

        clear = MergeClearFactory(ticket=None, pr_id=99, slug="statusline-stale-wakeup")
        with patch(_CLONE_SLUG, return_value=""):
            verdict = classify(clear, read=counted)

        assert verdict is ClearLiveness.UNVERIFIED
        assert reads == []

    def test_a_workstream_slug_resolves_through_the_clone_origin(self) -> None:
        # The repo comes from the canonical resolution chain, not the slug alone — a
        # workstream-slugged CLEAR is still checkable when the clone names its repo.
        clear = MergeClearFactory(ticket=None, pr_id=99, slug="statusline-stale-wakeup")
        with patch(_CLONE_SLUG, return_value="souliane/teatree"):
            assert classify(clear, read=_reads(PrOpenState.MERGED)) is ClearLiveness.MERGED

    def test_the_url_names_the_repo_and_the_pr(self) -> None:
        assert clear_pr_url(self._clear(pr_id=4142)) == "https://github.com/souliane/teatree/pull/4142"


class ProbeTests(django.test.TestCase):
    def _clears(self, count: int) -> list[object]:
        return [MergeClearFactory(ticket=None, pr_id=7000 + offset, slug="souliane/teatree") for offset in range(count)]

    def test_it_classifies_every_row_under_the_cap(self) -> None:
        result = probe(self._clears(3), read=_reads(PrOpenState.OPEN), cap=10)

        assert len(result.of(ClearLiveness.STALLED)) == 3
        assert result.unprobed == ()

    def test_it_carries_the_rows_the_cap_left_unread(self) -> None:
        result = probe(self._clears(5), read=_reads(PrOpenState.MERGED), cap=2)

        assert len(result.verdicts) == 2
        assert len(result.unprobed) == 3

    def test_of_selects_across_several_verdicts(self) -> None:
        rows = self._clears(2)
        states = iter([PrOpenState.MERGED, PrOpenState.CLOSED])

        result = probe(rows, read=lambda _url: next(states), cap=10)

        assert result.of(ClearLiveness.MERGED, ClearLiveness.ABANDONED) == rows
        assert result.of(ClearLiveness.STALLED) == []
