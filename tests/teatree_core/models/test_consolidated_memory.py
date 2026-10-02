"""Consolidation ledger tests (#1933).

The ledger advances each member cluster through a monotonic status ladder
(CANDIDATE → VERIFIED → PROMOTED, plus SUPERSEDED / EXPIRED retirement)
whose every rung has a production writer — recording a cited cluster, a wider
cluster covering a narrower row, and the merged fix of a core gap —
is idempotent on ``cluster_key`` so re-clustering the same members never
duplicates a row, refuses to leave CANDIDATE without a cited mistake, and
never silently drops BINDING feedback on expire.
"""

import hashlib

import pytest
from django.test import TestCase

from teatree.core.models import BindingFeedbackError, ConsolidatedMemory


def _key(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _record(seed: str = "members-a", *, is_binding: bool = False, overlay: str = "acme") -> ConsolidatedMemory:
    return ConsolidatedMemory.record_cluster(
        cluster_key=_key(seed),
        rule="Always run the gate before pushing.",
        source_files=[{"path": "MEMORY.md", "span": [1, 4]}],
        member_count=3,
        max_member_weight=5,
        is_binding=is_binding,
        overlay=overlay,
    )


class TestRecordCluster(TestCase):
    def test_records_candidate_with_fields(self) -> None:
        row = _record()

        assert row.status == ConsolidatedMemory.Status.CANDIDATE
        assert row.rule == "Always run the gate before pushing."
        assert row.source_files == [{"path": "MEMORY.md", "span": [1, 4]}]
        assert row.member_count == 3
        assert row.max_member_weight == 5

    def test_persists_durable_destination_on_create(self) -> None:
        row = ConsolidatedMemory.record_cluster(
            cluster_key=_key("dd"),
            rule="Always run the gate before pushing.",
            source_files=[],
            member_count=1,
            max_member_weight=5,
            is_binding=False,
            durable_destination="src/teatree/loops/dream/engine.py",
        )

        row.refresh_from_db()
        assert row.durable_destination == "src/teatree/loops/dream/engine.py"

    def test_durable_destination_defaults_empty(self) -> None:
        row = _record()

        assert row.durable_destination == ""

    def test_is_idempotent_on_cluster_key(self) -> None:
        first = _record("same-members")
        again = ConsolidatedMemory.record_cluster(
            cluster_key=_key("same-members"),
            rule="A different distilled rule",
            source_files=[],
            member_count=99,
            max_member_weight=99,
            is_binding=False,
        )

        assert again.pk == first.pk
        assert ConsolidatedMemory.objects.count() == 1
        again.refresh_from_db()
        assert again.rule == "Always run the gate before pushing."
        assert again.member_count == 3

    def test_distinct_cluster_key_is_a_new_row(self) -> None:
        first = _record("members-a")
        other = _record("members-b")

        assert first.pk != other.pk
        assert ConsolidatedMemory.objects.count() == 2


class TestStatusLadder(TestCase):
    def test_mark_verified_sets_citation_and_status(self) -> None:
        row = _record()

        row.mark_verified("Pushed without running the gate on 2026-06-01")

        row.refresh_from_db()
        assert row.status == ConsolidatedMemory.Status.VERIFIED
        assert row.verified_citation == "Pushed without running the gate on 2026-06-01"

    def test_mark_verified_refuses_empty_citation(self) -> None:
        row = _record()

        with pytest.raises(ValueError, match="non-empty citation"):
            row.mark_verified("   ")

        row.refresh_from_db()
        assert row.status == ConsolidatedMemory.Status.CANDIDATE
        assert row.verified_citation == ""

    def test_mark_promoted_sets_destination_and_timestamp(self) -> None:
        row = _record()
        row.mark_verified("a real cited mistake")

        row.mark_promoted("memory/topic_gate_discipline.md")

        row.refresh_from_db()
        assert row.status == ConsolidatedMemory.Status.PROMOTED
        assert row.durable_destination == "memory/topic_gate_discipline.md"
        assert row.promoted_at is not None

    def test_supersede_points_at_replacement(self) -> None:
        old = _record("old")
        new = _record("new")

        old.supersede(new)

        old.refresh_from_db()
        assert old.status == ConsolidatedMemory.Status.SUPERSEDED
        assert old.superseded_by_id == new.pk
        assert new.supersedes.filter(pk=old.pk).exists()

    def test_expire_sets_archive_path_and_timestamp(self) -> None:
        row = _record()

        row.expire("archive/2026/q2/expired.md")

        row.refresh_from_db()
        assert row.status == ConsolidatedMemory.Status.EXPIRED
        assert row.archive_path == "archive/2026/q2/expired.md"
        assert row.expired_at is not None


class TestBindingExpireRefusal(TestCase):
    def test_expire_raises_for_binding_feedback(self) -> None:
        row = _record(is_binding=True)

        with pytest.raises(BindingFeedbackError):
            row.expire("archive/path.md")

        row.refresh_from_db()
        assert row.status == ConsolidatedMemory.Status.CANDIDATE
        assert row.expired_at is None
        assert row.archive_path == ""


class TestCanPruneIndexLine(TestCase):
    def test_candidate_cannot_be_pruned(self) -> None:
        row = _record()
        assert row.can_prune_index_line is False

    def test_promoted_with_destination_can_be_pruned(self) -> None:
        row = _record()
        row.mark_verified("cited mistake")
        row.mark_promoted("memory/home.md")

        assert row.can_prune_index_line is True

    def test_terminal_without_destination_cannot_be_pruned(self) -> None:
        row = _record()
        row.expire("archive/path.md")

        assert row.status == ConsolidatedMemory.Status.EXPIRED
        assert row.durable_destination == ""
        assert row.can_prune_index_line is False


class TestManager(TestCase):
    def test_prunable_returns_only_terminal_rows_with_destination(self) -> None:
        candidate = _record("c1")
        promoted = _record("c2")
        promoted.mark_verified("m")
        promoted.mark_promoted("memory/home.md")
        terminal_no_dest = _record("c3")
        terminal_no_dest.expire("archive/x.md")

        prunable = list(ConsolidatedMemory.objects.prunable())

        assert promoted in prunable
        assert candidate not in prunable
        assert terminal_no_dest not in prunable

    def test_verified_for_overlay_scopes_to_verified_and_overlay(self) -> None:
        verified = _record("v", overlay="acme")
        verified.mark_verified("m")
        _record("c", overlay="acme")
        other_overlay = _record("o", overlay="widgets")
        other_overlay.mark_verified("m")

        result = list(ConsolidatedMemory.objects.verified_for_overlay("acme"))

        assert result == [verified]


class TestDispositionLadder(TestCase):
    """Pass-2 (#2426) drains the ledger via a disposition ladder, never silently dropping BINDING."""

    def test_default_disposition_is_untriaged(self) -> None:
        assert _record().disposition == ConsolidatedMemory.Disposition.UNTRIAGED

    def test_classify_user_specific_keeps_the_memory(self) -> None:
        row = _record()
        row.classify_user_specific()
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.USER_SPECIFIC_KEEP

    def test_classify_core_gap_queues_for_ticketing(self) -> None:
        row = _record()
        row.classify_core_gap()
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET

    def test_mark_ticketed_records_url_and_advances(self) -> None:
        row = _record()
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert row.ticket_url == "https://github.com/souliane/teatree/issues/42"

    def test_mark_ticketed_refuses_empty_url(self) -> None:
        row = _record()
        row.classify_core_gap()
        with pytest.raises(ValueError, match="non-empty ticket URL"):
            row.mark_ticketed("  ")
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET
        assert row.ticket_url == ""

    def test_retire_archives_the_prose(self) -> None:
        row = _record()
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        row.retire("https://github.com/souliane/teatree/issues/42")
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED
        assert row.archive_path == "https://github.com/souliane/teatree/issues/42"
        assert row.expired_at is not None

    def test_reopen_core_gap_returns_a_ticketed_row_to_the_queue(self) -> None:
        row = _record()
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/2663#dream-batch=abc")
        row.reopen_core_gap()
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET
        assert row.ticket_url == ""
        assert ConsolidatedMemory.objects.needs_ticket().filter(pk=row.pk).exists()

    def test_reopen_core_gap_refuses_a_row_that_is_not_ticketed(self) -> None:
        row = _record()
        row.classify_core_gap()
        with pytest.raises(ValueError, match="TICKETED"):
            row.reopen_core_gap()
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET

    def test_retire_refuses_binding_row(self) -> None:
        row = _record(is_binding=True)
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        with pytest.raises(BindingFeedbackError):
            row.retire("archive/x.md")
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED


class TestCitedClusterIsRecordedVerified(TestCase):
    """A citation proven at the door is the VERIFIED rung — no second trust step."""

    def test_a_recorded_citation_lands_the_row_verified(self) -> None:
        row = ConsolidatedMemory.record_cluster(
            cluster_key=_key("cited"),
            rule="Always run the gate before pushing.",
            source_files=["MEMORY.md"],
            member_count=1,
            max_member_weight=5,
            is_binding=False,
            verified_citation="Pushed without running the gate on 2026-06-01",
        )

        row.refresh_from_db()
        assert row.status == ConsolidatedMemory.Status.VERIFIED
        assert row.verified_citation == "Pushed without running the gate on 2026-06-01"

    def test_an_uncited_row_still_lands_candidate(self) -> None:
        assert _record("uncited").status == ConsolidatedMemory.Status.CANDIDATE


class TestSupersedeCoveredBy(TestCase):
    """A wider re-cluster takes over the untriaged row it covers, without homing either."""

    def _row(self, seed: str, paths: list[object], *, is_binding: bool = False) -> ConsolidatedMemory:
        return ConsolidatedMemory.record_cluster(
            cluster_key=_key(seed),
            rule="Always run the gate before pushing.",
            source_files=paths,
            member_count=len(paths),
            max_member_weight=5,
            is_binding=is_binding,
            overlay="acme",
            verified_citation="a cited mistake",
        )

    def test_a_strict_superset_supersedes_the_row_it_covers(self) -> None:
        narrow = self._row("narrow", ["a.jsonl"])
        wide = self._row("wide", ["a.jsonl", "b.jsonl"])

        assert ConsolidatedMemory.objects.supersede_covered_by(wide) == [narrow]
        narrow.refresh_from_db()
        assert narrow.status == ConsolidatedMemory.Status.SUPERSEDED
        assert narrow.superseded_by_id == wide.pk

    def test_a_partial_overlap_supersedes_nothing(self) -> None:
        other = self._row("overlap", ["a.jsonl", "c.jsonl"])
        wide = self._row("wide", ["a.jsonl", "b.jsonl"])

        assert ConsolidatedMemory.objects.supersede_covered_by(wide) == []
        other.refresh_from_db()
        assert other.status == ConsolidatedMemory.Status.VERIFIED

    def test_a_binding_row_is_never_superseded(self) -> None:
        binding = self._row("binding", ["a.jsonl"], is_binding=True)
        wide = self._row("wide", ["a.jsonl", "b.jsonl"])

        assert ConsolidatedMemory.objects.supersede_covered_by(wide) == []
        binding.refresh_from_db()
        assert binding.status == ConsolidatedMemory.Status.VERIFIED

    def test_a_row_from_another_overlay_is_never_superseded(self) -> None:
        other_overlay = ConsolidatedMemory.record_cluster(
            cluster_key=_key("other-overlay"),
            rule="Always run the gate before pushing.",
            source_files=["a.jsonl"],
            member_count=1,
            max_member_weight=5,
            is_binding=False,
            overlay="other",
            verified_citation="a cited mistake",
        )
        wide = self._row("wide", ["a.jsonl", "b.jsonl"])

        assert ConsolidatedMemory.objects.supersede_covered_by(wide) == []
        other_overlay.refresh_from_db()
        assert other_overlay.status == ConsolidatedMemory.Status.VERIFIED

    def test_an_already_terminal_row_is_left_where_it_is(self) -> None:
        promoted = self._row("promoted", ["a.jsonl"])
        promoted.mark_promoted("skills/rules/SKILL.md")
        wide = self._row("wide", ["a.jsonl", "b.jsonl"])

        assert ConsolidatedMemory.objects.supersede_covered_by(wide) == []
        promoted.refresh_from_db()
        assert promoted.status == ConsolidatedMemory.Status.PROMOTED

    def test_a_row_pass_two_already_routed_keeps_its_decision(self) -> None:
        kept = self._row("kept", ["a.jsonl"])
        kept.classify_user_specific()
        wide = self._row("wide", ["a.jsonl", "b.jsonl"])

        assert ConsolidatedMemory.objects.supersede_covered_by(wide) == []
        kept.refresh_from_db()
        assert kept.status == ConsolidatedMemory.Status.VERIFIED

    def test_a_superseded_row_is_not_a_durable_home(self) -> None:
        narrow = ConsolidatedMemory.record_cluster(
            cluster_key=_key("narrow-with-hint"),
            rule="Always run the gate before pushing.",
            source_files=["a.md"],
            member_count=1,
            max_member_weight=5,
            is_binding=False,
            overlay="acme",
            durable_destination="feedback/run_gate.md",
            verified_citation="a cited mistake",
        )
        wide = self._row("wide", ["a.md", "b.md"])

        ConsolidatedMemory.objects.supersede_covered_by(wide)

        narrow.refresh_from_db()
        assert narrow.status == ConsolidatedMemory.Status.SUPERSEDED
        assert narrow.can_prune_index_line is False
        assert list(ConsolidatedMemory.objects.prunable()) == []

    def test_member_paths_reads_both_stored_member_shapes(self) -> None:
        row = self._row("shapes", ["a.jsonl", {"path": "b.jsonl", "span": [1, 4]}, 7])

        assert row.member_paths == frozenset({"a.jsonl", "b.jsonl"})


class TestRetireWalksTheTrustLadder(TestCase):
    """The merged fix homes the rule and retires the prose — what feeds the decay rail."""

    def _ticketed(self) -> ConsolidatedMemory:
        row = ConsolidatedMemory.record_cluster(
            cluster_key=_key("gap"),
            rule="A gate must fail loud, never skip as pass.",
            source_files=["a.jsonl"],
            member_count=1,
            max_member_weight=5,
            is_binding=False,
            durable_destination="src/teatree/core/gates/rubric_gate.py",
            verified_citation="a cited mistake",
        )
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        return row

    def test_retire_promotes_the_rule_and_expires_the_prose(self) -> None:
        row = self._ticketed()

        row.retire("https://github.com/souliane/teatree/issues/42")

        row.refresh_from_db()
        assert row.status == ConsolidatedMemory.Status.EXPIRED
        assert row.promoted_at is not None
        assert row.durable_destination == "src/teatree/core/gates/rubric_gate.py"

    def test_a_retired_row_is_what_the_prune_rail_reads(self) -> None:
        row = self._ticketed()

        row.retire("https://github.com/souliane/teatree/issues/42")

        assert list(ConsolidatedMemory.objects.prunable()) == [row]

    def test_a_ticketed_row_is_not_prunable_before_its_fix_lands(self) -> None:
        self._ticketed()

        assert list(ConsolidatedMemory.objects.prunable()) == []


class TestDispositionManager(TestCase):
    def test_untriaged_returns_only_unclassified_rows(self) -> None:
        fresh = _record("u1")
        classified = _record("u2")
        classified.classify_user_specific()
        result = list(ConsolidatedMemory.objects.untriaged())
        assert fresh in result
        assert classified not in result

    def test_awaiting_ticket_close_returns_only_ticketed_rows_with_url(self) -> None:
        ticketed = _record("t1")
        ticketed.classify_core_gap()
        ticketed.mark_ticketed("https://github.com/souliane/teatree/issues/1")
        gap_no_ticket = _record("t2")
        gap_no_ticket.classify_core_gap()
        result = list(ConsolidatedMemory.objects.awaiting_ticket_close())
        assert ticketed in result
        assert gap_no_ticket not in result

    def test_needs_ticket_returns_core_gaps_with_no_ticket_yet(self) -> None:
        # F6.1(b): the promote-pass drain queue — a core gap classified but not yet
        # promoted (no ticket recorded). Untriaged rows and already-ticketed rows are
        # excluded, so a re-run drains exactly the stranded gaps.
        stranded = _record("n1")
        stranded.classify_core_gap()  # CORE_GAP_NEEDS_TICKET, no ticket
        ticketed = _record("n2")
        ticketed.classify_core_gap()
        ticketed.mark_ticketed("https://github.com/souliane/teatree/issues/2")
        untriaged = _record("n3")
        user_specific = _record("n4")
        user_specific.classify_user_specific()
        result = list(ConsolidatedMemory.objects.needs_ticket())
        assert stranded in result
        assert ticketed not in result
        assert untriaged not in result
        assert user_specific not in result

    def test_schema_count_counts_overlay_rows(self) -> None:
        _record("a", overlay="acme")
        _record("b", overlay="acme")
        _record("c", overlay="widgets")

        assert ConsolidatedMemory.objects.schema_count("acme") == 2
        assert ConsolidatedMemory.objects.schema_count("widgets") == 1


class TestStr(TestCase):
    def test_renders_pk_status_and_rule(self) -> None:
        row = _record()

        rendered = str(row)

        assert rendered.startswith(f"consolidated-memory<{row.pk}:{ConsolidatedMemory.Status.CANDIDATE}:")
        assert "Always run the gate before pushing." in rendered
