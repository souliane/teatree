"""write_clusters must reject re-promoting a recurring MEMORY_ONLY cluster (#2663).

The root-KPI rule: a rule that already has a durable memory and recurs must NOT
produce another memory — it reclassifies to a core-gap/escalation. These tests
drive that guard through the real ledger-promotion path (`write_clusters`) so the
keep-as-memory branch cannot silently re-promote a recurrence.
"""

from pathlib import Path

from django.test import TestCase

from teatree.core.models import ConsolidatedMemory, InstructionComplianceRecord, RuleSource
from teatree.loops.dream.compliance import reclassify_recurring_memory_clusters
from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, WeightedSnippet, write_clusters

_SLUG = "feedback_askuserquestion_overuse"
_MEMORY_PATH = f"/memory/{_SLUG}.md"


def _record_recurrence(slug: str = _SLUG) -> None:
    InstructionComplianceRecord.objects.create(
        rule_source=RuleSource.MEMORY,
        rule_identity=slug,
        evidence="violated again",
        is_recurrence=True,
    )


def _memory_only_cluster(citation: str = "Do not fire AskUserQuestion for routine obstacles") -> DistilledCluster:
    return DistilledCluster(
        cluster_key="ckey-1",
        rule="Do not fire AskUserQuestion for routine obstacles.",
        source_files=[_MEMORY_PATH],
        is_binding=False,
        verified_citation=citation,
        durable_destination="feedback/askuserquestion.md",
    )


def _extract_for(_cluster: DistilledCluster) -> ConsolidationExtract:
    return ConsolidationExtract(
        snippets=(
            WeightedSnippet(
                path=Path(_MEMORY_PATH),
                kind="memory",
                weight=90,
                text=f"name: {_SLUG}\nDo not fire AskUserQuestion for routine obstacles.\n",
            ),
        ),
        truncated=False,
    )


class ReclassifyRecurringMemoryClustersTestCase(TestCase):
    """A MEMORY_ONLY cluster whose rule already recurred is redirected off the memory destination."""

    def test_recurring_memory_cluster_is_reclassified_to_core_gap(self) -> None:
        _record_recurrence()
        cluster = _memory_only_cluster()
        [reclassified] = reclassify_recurring_memory_clusters([cluster])
        # No longer a memory destination — it points at a teatree-core fix path (core-gap).
        assert not reclassified.durable_destination.startswith("feedback/")
        assert reclassified.durable_destination.startswith(("src/teatree", "skills/"))

    def test_non_recurring_memory_cluster_is_left_as_memory(self) -> None:
        # No recurrence on record → the keep-as-memory destination is legitimate, untouched.
        cluster = _memory_only_cluster()
        [unchanged] = reclassify_recurring_memory_clusters([cluster])
        assert unchanged.durable_destination == "feedback/askuserquestion.md"

    def test_already_core_destination_is_untouched_even_on_recurrence(self) -> None:
        _record_recurrence()
        core = DistilledCluster(
            cluster_key="ckey-core",
            rule="Run the gate before push.",
            source_files=[_MEMORY_PATH],
            is_binding=False,
            verified_citation="x",
            durable_destination="src/teatree/loops/gate.py",
        )
        [unchanged] = reclassify_recurring_memory_clusters([core])
        assert unchanged.durable_destination == "src/teatree/loops/gate.py"


class WriteClustersRecurrenceGuardTestCase(TestCase):
    """write_clusters routes a recurring MEMORY_ONLY cluster to a core-gap destination, not a memory."""

    def test_recurring_memory_cluster_lands_as_core_gap_not_memory(self) -> None:
        _record_recurrence()
        cluster = _memory_only_cluster()
        outcome = write_clusters([cluster], _extract_for(cluster), dry_run=False)
        assert outcome.written == 1
        row = ConsolidatedMemory.objects.get(cluster_key="ckey-1")
        # Persisted with a teatree-core destination — so Pass-2 triage tickets it as
        # a core gap instead of re-promoting another memory.
        assert not row.durable_destination.startswith("feedback/")
        assert row.durable_destination.startswith(("src/teatree", "skills/"))

    def test_non_recurring_memory_cluster_keeps_its_memory_destination(self) -> None:
        cluster = _memory_only_cluster()
        write_clusters([cluster], _extract_for(cluster), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="ckey-1")
        assert row.durable_destination == "feedback/askuserquestion.md"
