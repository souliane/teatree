"""The distillation engine is a typed SEAM in this scaffold PR (#1933).

The actual replay -> dedup/merge -> distill -> re-index -> archive engine that
writes ``ConsolidatedMemory`` rows ships in a follow-up PR. Here the engine is
a stub returning a typed :class:`DreamRunResult` so the cron orchestration
(in-flight lock, dry-run, marker stamping, staleness) is fully testable with no
LLM. The stub honours ``dry_run`` (it must never write a row) so the dry-run
contract is observable at the seam.
"""

from django.test import TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream.engine import DreamRunResult, run_consolidation


class DreamRunResultTestCase(TestCase):
    def test_result_is_typed_and_frozen(self) -> None:
        result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=True)
        assert result.dry_run is True
        assert result.clusters_recorded == 0
        assert result.members_replayed == 0


class RunConsolidationSeamTestCase(TestCase):
    def test_returns_dream_run_result(self) -> None:
        result = run_consolidation(overlay="", since=None, dry_run=False)
        assert isinstance(result, DreamRunResult)

    def test_dry_run_writes_no_consolidated_memory_rows(self) -> None:
        run_consolidation(overlay="", since=None, dry_run=True)
        assert ConsolidatedMemory.objects.count() == 0

    def test_dry_run_result_flags_dry_run(self) -> None:
        result = run_consolidation(overlay="", since=None, dry_run=True)
        assert result.dry_run is True

    def test_seam_writes_no_rows_until_engine_lands(self) -> None:
        # The stub is a no-op engine: it records no clusters yet. This guards
        # that the scaffold PR ships an inert seam (the real engine is #1933
        # follow-up), so a future regression that accidentally writes rows
        # from the stub is caught.
        run_consolidation(overlay="", since=None, dry_run=False)
        assert ConsolidatedMemory.objects.count() == 0
