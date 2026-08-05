"""Batching + broken-distiller accounting for the dream pass.

Two contracts live here. (1) A distiller whose reply is BROKEN — unauthenticated,
truncated, unparsable — must be told from a healthy "nothing to consolidate" and
must FAIL the pass, carrying the raw reply so the cause is readable without
instrumenting the code. (2) The prompt budget binds per BATCH, never per corpus:
every ranked member reaches a distiller call, and a per-pass batch cap defers its
remainder to the next pass instead of dropping it.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.loops.dream import distill
from teatree.loops.dream.engine import DistilledCluster, DistillEmptyReason, Distiller, DistillResult
from teatree.loops.dream.replay import ConsolidationExtract, TranscriptMember, build_extract

_CITATION = "the agent force-pushed to main and lost the branch"


def _memory_members(tmp: Path, count: int, *, chars: int = 4000) -> list[TranscriptMember]:
    members: list[TranscriptMember] = []
    for i in range(count):
        path = tmp / f"feedback_{i:04d}.md"
        path.write_text(f"BINDING: lesson {i} — {_CITATION} " + "x" * chars)
        members.append(TranscriptMember(path=path, kind="memory"))
    return members


def _healthy_empty(_batch: ConsolidationExtract) -> DistillResult:
    return DistillResult(clusters=[], empty_reason=DistillEmptyReason.NOTHING_TO_CONSOLIDATE)


def _unparsable(_batch: ConsolidationExtract) -> DistillResult:
    return DistillResult(clusters=[], empty_reason=DistillEmptyReason.UNPARSABLE)


class BrokenEmptyIsNotHealthyEmptyTestCase(TestCase):
    """`nothing_to_consolidate` is healthy; every other empty reason is broken output."""

    def test_nothing_to_consolidate_is_the_only_healthy_reason(self) -> None:
        assert DistillEmptyReason.NOTHING_TO_CONSOLIDATE.is_broken is False

    def test_unparsable_is_broken(self) -> None:
        assert DistillEmptyReason.UNPARSABLE.is_broken is True

    def test_empty_raw_is_broken(self) -> None:
        assert DistillEmptyReason.EMPTY_RAW.is_broken is True

    def test_all_entries_dropped_is_broken(self) -> None:
        assert DistillEmptyReason.ALL_ENTRIES_DROPPED.is_broken is True


class BrokenBatchIsCountedAndDiagnosedTestCase(TestCase):
    """A broken batch is counted apart from a healthy empty and carries its raw reply."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.extract = build_extract(_memory_members(self.tmp, 2))

    def test_unparsable_batch_counts_as_broken(self) -> None:
        outcome = distill.distill_in_batches(self.extract, distiller=_unparsable)
        assert outcome.broken_batches == 1

    def test_healthy_empty_batch_does_not_count_as_broken(self) -> None:
        outcome = distill.distill_in_batches(self.extract, distiller=_healthy_empty)
        assert outcome.broken_batches == 0

    def test_diagnostic_carries_the_raw_reply_that_failed_to_parse(self) -> None:
        # The reply that produced the real 0-cluster pass was the `claude` CLI's
        # unauthenticated refusal. Without it on the diagnostic an operator sees a
        # bare "unparsable" and cannot tell an auth gap from a model formatting slip.
        def _not_logged_in(_batch: ConsolidationExtract) -> DistillResult:
            return DistillResult(
                clusters=[],
                empty_reason=DistillEmptyReason.UNPARSABLE,
                raw_excerpt="Not logged in · Please run /login",
            )

        outcome = distill.distill_in_batches(self.extract, distiller=_not_logged_in)
        assert any("Not logged in" in line for line in outcome.diagnostics)

    def test_raising_batch_is_diagnosed_too(self) -> None:
        def _boom(_batch: ConsolidationExtract) -> DistillResult:
            msg = "claude is not installed"
            raise RuntimeError(msg)

        outcome = distill.distill_in_batches(self.extract, distiller=_boom)
        assert outcome.failed_batches == 1
        assert any("claude is not installed" in line for line in outcome.diagnostics)


class EveryMemberReachesADistillerCallTestCase(TestCase):
    """The prompt budget bounds a BATCH, never the corpus — no member is dropped."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Five prompt-ceilings' worth of corpus, so a single-prompt bound would
        # have to discard four fifths of it.
        self.members = _memory_members(self.tmp, 5 * ConsolidationExtract.CHAR_CEILING // 4000)

    def _distil_spy(self) -> tuple[list[str], list[int]]:
        seen: list[str] = []
        sizes: list[int] = []

        def _spy(batch: ConsolidationExtract) -> list[DistilledCluster]:
            seen.extend(str(snippet.path) for snippet in batch.snippets)
            sizes.append(sum(len(snippet.text) for snippet in batch.snippets))
            return []

        distill.distill_in_batches(build_extract(self.members), distiller=_spy)
        return seen, sizes

    def test_no_member_is_dropped_for_prompt_budget(self) -> None:
        seen, _ = self._distil_spy()
        assert set(seen) == {str(member.path) for member in self.members}

    def test_each_member_is_distilled_exactly_once(self) -> None:
        seen, _ = self._distil_spy()
        assert len(seen) == len(self.members)

    def test_every_batch_fits_the_prompt_ceiling(self) -> None:
        _, sizes = self._distil_spy()
        assert max(sizes) <= ConsolidationExtract.CHAR_CEILING

    def test_corpus_larger_than_one_prompt_produces_more_than_one_batch(self) -> None:
        _, sizes = self._distil_spy()
        assert len(sizes) > 1


class DeferredRemainderIsCarriedTestCase(TestCase):
    """When the per-pass batch cap binds, the remainder rides to the NEXT pass."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.extract = build_extract(_memory_members(self.tmp, 3 * ConsolidationExtract.CHAR_CEILING // 4000))

    def _run_capped_pass(self, distiller: Distiller | None = None) -> list[str]:
        """One capped pass, then the caller's half of the contract: commit the cursor.

        ``distill_in_batches`` only PROPOSES the advance;
        :func:`~teatree.loops.dream.engine.run_consolidation` commits it in the same
        transaction as the ledger write. Mirroring that split here is what keeps these
        tests exercising the real rotation rather than a side effect it no longer has.
        """
        seen: list[str] = []

        def _spy(batch: ConsolidationExtract) -> list[DistilledCluster]:
            seen.extend(str(snippet.path) for snippet in batch.snippets)
            return []

        with patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_BATCHES": "1"}):
            self.outcome = distill.distill_in_batches(self.extract, distiller=distiller or _spy)
        if self.outcome.next_cursor is not None:
            distill.commit_distill_cursor(self.outcome.next_cursor)
        return seen

    def test_capped_pass_reports_its_deferred_remainder(self) -> None:
        self._run_capped_pass()
        assert self.outcome.deferred_members > 0

    def test_next_pass_distils_members_the_capped_pass_deferred(self) -> None:
        first = set(self._run_capped_pass())
        second = set(self._run_capped_pass())
        assert second - first, "the second pass repeated the first pass's members and made no progress"

    def test_uncapped_pass_defers_nothing(self) -> None:
        def _spy(_batch: ConsolidationExtract) -> list[DistilledCluster]:
            return []

        outcome = distill.distill_in_batches(self.extract, distiller=_spy)
        assert outcome.deferred_members == 0

    def test_a_capped_pass_only_proposes_the_advance_it_never_writes_it(self) -> None:
        """The cursor must not move on ``distill_in_batches`` alone — the ledger write owns it.

        Writing it here put it in its own autocommit ahead of ``write_clusters``, so
        anything raising in that window left a cursor claiming a window was consolidated
        with no rows to show for it, and the rotation only revisits it after wrapping the
        whole corpus.
        """
        before = distill._read_cursor()

        with patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_BATCHES": "1"}):
            outcome = distill.distill_in_batches(self.extract, distiller=_healthy_empty)

        assert outcome.next_cursor is not None
        assert distill._read_cursor() == before

    def test_a_dry_run_proposes_no_advance(self) -> None:
        with patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_BATCHES": "1"}):
            outcome = distill.distill_in_batches(self.extract, distiller=_healthy_empty, dry_run=True)

        assert outcome.next_cursor is None


class ABrokenPassHoldsTheCursorTestCase(TestCase):
    """An outage must not walk the cursor across the corpus, skipping all of it.

    ``_distil_one`` swallows a raise and a broken reply on purpose, so one bad call
    cannot discard the paid work of the other batches. Advancing the cursor anyway
    turned that isolation into silent data loss: an unauthenticated ``claude``
    answering ``Not logged in`` fails EVERY batch, and each pass moved the rotation on
    while reporting nothing worse than "0 clusters".
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.extract = build_extract(_memory_members(self.tmp, 3 * ConsolidationExtract.CHAR_CEILING // 4000))

    def _capped_outcome(self, distiller: Distiller) -> distill.BatchDistillOutcome:
        with patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_BATCHES": "1"}):
            return distill.distill_in_batches(self.extract, distiller=distiller)

    def test_a_broken_reply_holds_the_cursor(self) -> None:
        outcome = self._capped_outcome(_unparsable)

        assert outcome.broken_batches == 1
        assert outcome.next_cursor is None

    def test_a_raising_batch_holds_the_cursor(self) -> None:
        def _raises(_batch: ConsolidationExtract) -> DistillResult:
            msg = "Not logged in · Please run /login"
            raise RuntimeError(msg)

        outcome = self._capped_outcome(_raises)

        assert outcome.failed_batches == 1
        assert outcome.next_cursor is None

    def test_a_healthy_empty_reply_still_advances(self) -> None:
        """A healthy empty reply is an ANSWER — the window was reached, so it may be passed."""
        outcome = self._capped_outcome(_healthy_empty)

        assert outcome.broken_batches == 0
        assert outcome.next_cursor is not None
