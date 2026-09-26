"""Dreaming Pass 2 — promote core-generic memories into teatree fixes (#2426).

Pass 1 writes consolidated rules into the ``ConsolidatedMemory`` ledger; on its
own that is "retro with a database". Pass 2 drains the ledger: it triages each
row as user-specific (legitimately stays as memory) or core-generic (a confession
that teatree core has a workflow gap), files a teatree backlog ticket for the
core-generic ones, and retires the prose once the linked fix lands.

These tests drive the classify → ticket → retire lifecycle with an INJECTED
classifier and a fake code host, so Pass 2 is fully testable without an LLM and
without a live forge.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.ticket import Ticket
from teatree.loops.dream import batch_promote as bp_module
from teatree.loops.dream.batch_promote import PromotionBatch
from teatree.loops.dream.merge import BindingConflict
from teatree.loops.dream.promote_memory import (
    MemoryDisposition,
    delete_source_memory_files,
    file_binding_reconciliation_tickets,
    file_core_gap_tickets,
    retire_resolved_memories,
    triage_disposition,
)
from teatree.loops.dream.retro_finding import promote_finding, record_finding


def _row(
    *,
    destination: str = "skills/ship/SKILL.md",
    binding: bool = False,
    source_files: list | None = None,
) -> ConsolidatedMemory:
    # No caller ever overrides these — kept fixed so the fixture stays under the
    # 5-kwarg complexity cap without a suppression (ac-django-no-complexity-suppressions).
    return ConsolidatedMemory.objects.create(
        cluster_key="k1",
        rule="Run the tree-wide health gate before any push.",
        source_files=source_files if source_files is not None else ["feedback_run_gate.md"],
        durable_destination=destination,
        is_binding=binding,
        member_count=1,
        max_member_weight=90,
        verified_citation="pushed without running the gate, CI went red",
    )


UMBRELLA = "https://github.com/souliane/teatree/issues/2663"


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.search_open_issues.return_value = []
    host.get_issue.return_value = {"body": body}
    host.update_issue.return_value = {"number": 2663}
    return host


class TriageDispositionTestCase(TestCase):
    """The default classifier reads the durable_destination hint to split the two kinds."""

    def test_skill_destination_is_a_core_gap(self) -> None:
        # A rule whose durable home is a teatree skill/code path is generic teatree
        # doctrine — a workflow gap to fix in code, not a personal memory.
        row = _row(destination="skills/ship/SKILL.md")
        assert triage_disposition(row) is MemoryDisposition.CORE_GAP

    def test_src_destination_is_a_core_gap(self) -> None:
        row = _row(destination="src/teatree/core/gates.py")
        assert triage_disposition(row) is MemoryDisposition.CORE_GAP

    def test_personal_memory_destination_is_user_specific(self) -> None:
        # A rule whose home is a personal memory topic file is user-specific.
        row = _row(destination="feedback/editor_preference.md")
        assert triage_disposition(row) is MemoryDisposition.USER_SPECIFIC

    def test_empty_destination_is_user_specific_conservative(self) -> None:
        # No durable-home hint → keep as memory (conservative: never file a ticket
        # for a row we cannot confidently classify as a teatree-core gap).
        row = _row(destination="")
        assert triage_disposition(row) is MemoryDisposition.USER_SPECIFIC

    def test_memory_shaped_destination_is_now_a_core_gap(self) -> None:
        # #4776: before this, EVERY memory/<slug>.md destination was ungrounded and
        # kept as memory forever — 49 of 49 gaps measured on one pass were withheld
        # this exact way, so batching had nothing to batch. A memory-shaped
        # destination now promotes.
        row = _row(destination="memory/push-success-must-be-verified-by-ls-remote.md")
        assert triage_disposition(row) is MemoryDisposition.CORE_GAP


class FileCoreGapTicketsTestCase(TestCase):
    """Core-gap rows queue into the pass's batch — never a triage issue, never alone (#4776)."""

    def test_core_gap_row_upserts_a_checkbox_and_schedules_a_fix(self) -> None:
        from teatree.core.models.task import Task  # noqa: PLC0415

        row = _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        batch = PromotionBatch()
        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET
        # Nothing is written/scheduled until the pass mints its single batch ticket.
        host.create_issue.assert_not_called()
        host.update_issue.assert_not_called()

        bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        # No fresh needs-triage issue is filed — the gap rides the umbrella + a coding task.
        host.create_issue.assert_not_called()
        host.update_issue.assert_called_once()
        assert Ticket.objects.filter(extra__dream_gap_batch__isnull=False).exists()
        assert Task.objects.filter(phase="coding").exists()

    def test_user_specific_row_is_classified_and_files_nothing(self) -> None:
        row = _row(destination="feedback/tone.md")
        batch = PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.USER_SPECIFIC_KEEP
        assert batch.pending == []

    def test_checkbox_carries_the_gap_marker(self) -> None:
        _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        batch = PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        _, kwargs = host.update_issue.call_args
        assert "<!-- dream-gap k1 -->" in kwargs["body"]

    def test_already_scheduled_gap_is_not_double_added(self) -> None:
        existing = "## Open gaps\n- [ ] Workflow gap (dreaming Pass 2): Run the tree-wide ... <!-- dream-gap k1 -->\n"
        _row(destination="skills/ship/SKILL.md")
        host = _fake_host(body=existing)
        batch = PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        outcome = bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        # The checkbox is already present — no rewrite.
        assert outcome.checkboxes_added == 0
        host.update_issue.assert_not_called()

    def test_banned_term_title_is_withheld_not_promoted(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        _row(destination="skills/ship/SKILL.md")
        batch = PromotionBatch()
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="customer-name"):
            outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        assert outcomes[0].filed is False
        assert outcomes[0].withheld is True
        assert batch.pending == []
        assert not Ticket.objects.filter(extra__dream_gap_key="k1").exists()

    def test_dry_run_writes_nothing_and_never_strands_the_gap(self) -> None:
        # F6.1: a preview must NOT advance the disposition. Advancing it before the
        # dry-run guard moved the row out of untriaged() while its promotion was
        # skipped, so the gap sat in CORE_GAP_NEEDS_TICKET with no drain — detected but
        # never fixed. A dry run now leaves the row UNTRIAGED so the next real pass
        # drains it faithfully, and queues no gap.
        row = _row(destination="skills/ship/SKILL.md")
        batch = PromotionBatch()
        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch, dry_run=True)
        row.refresh_from_db()
        assert outcomes == []
        assert row.disposition == ConsolidatedMemory.Disposition.UNTRIAGED
        assert batch.pending == []

    def test_stranded_core_gap_row_is_drained_and_promoted(self) -> None:
        # F6.1(b): a row a PRIOR pass classified CORE_GAP_NEEDS_TICKET but never
        # promoted (no ticket recorded) is drained by needs_ticket() and driven onto
        # the umbrella — so a detected gap can never sit un-promoted forever.
        from teatree.core.models.task import Task  # noqa: PLC0415

        row = _row(destination="skills/ship/SKILL.md")
        row.classify_core_gap()  # prior pass left it here with no ticket + no promotion
        assert not ConsolidatedMemory.objects.untriaged().exists()  # not in the untriaged queue
        host = _fake_host()
        batch = PromotionBatch()
        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        host.update_issue.assert_called_once()
        assert Ticket.objects.filter(extra__dream_gap_batch__isnull=False).exists()
        assert Task.objects.filter(phase="coding").exists()

    def test_a_new_core_gap_is_not_double_promoted_in_one_pass(self) -> None:
        # The needs_ticket() drain reads the queue BEFORE the untriaged loop classifies
        # a new core gap, so a row classified this pass is promoted exactly once, never
        # re-drained into a second outcome.
        _row(destination="skills/ship/SKILL.md")
        batch = PromotionBatch()
        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        assert len(outcomes) == 1

    def test_a_promoted_gap_is_stamped_with_its_batch_ticket_and_leaves_the_queue(self) -> None:
        row = _row()
        host = _fake_host()
        batch = PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)

        ticket = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).get()
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert row.ticket_url == ticket.issue_url
        assert "#dream-batch=" in row.ticket_url
        assert not ConsolidatedMemory.objects.needs_ticket().exists()

        next_batch = PromotionBatch()
        assert file_core_gap_tickets(umbrella_url=UMBRELLA, batch=next_batch) == []
        assert next_batch.already_covered == 0

    def test_a_row_already_riding_a_legacy_ticket_is_backfilled_once(self) -> None:
        row = _row()
        row.classify_core_gap()
        legacy = Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-gap=k1",
            role=Ticket.Role.AUTHOR,
            short_description="Fix the gate",
            extra={"dream_gap_key": "k1", "dream_memory_cluster_key": "k1", "dream_umbrella_url": UMBRELLA},
        )

        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=PromotionBatch())

        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert row.ticket_url == legacy.issue_url

    def _batch_ticket_covering_k1(self, **extra: object) -> Ticket:
        return Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-batch=covering",
            role=Ticket.Role.AUTHOR,
            short_description="Dream batch",
            extra={"dream_gap_batch": [{"gap_key": "k1", "cluster_key": "k1"}], **extra},
        )

    def test_a_row_is_never_attached_to_a_reconciled_ticket(self) -> None:
        row = _row()
        row.classify_core_gap()
        self._batch_ticket_covering_k1(
            dream_gap_reconciled_at="2026-01-01T00:00:00", dream_gap_claimed_delivered=["k1"]
        )

        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=PromotionBatch())

        self._assert_still_queued(row)

    def test_a_withheld_row_is_never_back_filled_onto_its_covering_ticket(self) -> None:
        row = _row()
        row.classify_core_gap()
        self._batch_ticket_covering_k1()

        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="customer-name"):
            file_core_gap_tickets(umbrella_url=UMBRELLA, batch=PromotionBatch())

        self._assert_still_queued(row)

    def _assert_still_queued(self, row: ConsolidatedMemory) -> None:
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET
        assert row.ticket_url == ""

    def test_a_withheld_gap_is_never_stamped(self) -> None:
        row = _row()
        row.classify_core_gap()
        batch = PromotionBatch()
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="customer-name"):
            file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        bp_module.promote_batch(_fake_host(), umbrella_url=UMBRELLA, batch=batch)
        self._assert_still_queued(row)

    def test_an_ungrounded_gap_is_never_stamped(self) -> None:
        row = _row(destination="src/teatree/ghost_pkg/ghost.py")
        row.classify_core_gap()
        batch = PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        bp_module.promote_batch(_fake_host(), umbrella_url=UMBRELLA, batch=batch)
        self._assert_still_queued(row)

    def test_a_dry_run_gap_is_never_stamped(self) -> None:
        row = _row()
        row.classify_core_gap()
        batch = PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch, dry_run=True)
        bp_module.promote_batch(_fake_host(), umbrella_url=UMBRELLA, batch=batch, dry_run=True)
        self._assert_still_queued(row)

    def test_a_failed_schedule_rolls_the_stamp_back(self) -> None:
        row = _row()
        batch = PromotionBatch()
        file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)
        with (
            patch.object(Ticket, "schedule_coding", side_effect=RuntimeError("queue down")),
            pytest.raises(RuntimeError),
        ):
            bp_module.promote_batch(_fake_host(), umbrella_url=UMBRELLA, batch=batch)
        assert ConsolidatedMemory.objects.needs_ticket().filter(pk=row.pk).exists()


def _conflict(survivor: str = "feedback_bind_one", absorbed: str = "feedback_bind_two") -> BindingConflict:
    return BindingConflict(
        survivor_name=survivor,
        absorbed_name=absorbed,
        survivor_path=Path(f"/m/{survivor}.md"),
        absorbed_path=Path(f"/m/{absorbed}.md"),
    )


class FileBindingReconciliationTicketsTestCase(TestCase):
    """Two conflicting BINDING memories get a deduped reconciliation ticket (#2723)."""

    def test_conflict_files_a_reconciliation_ticket(self) -> None:
        host = _fake_host()
        outcomes = file_binding_reconciliation_tickets(host, repo="souliane/teatree", conflicts=[_conflict()])
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        _, kwargs = host.create_issue.call_args
        assert "reconcil" in kwargs["body"].lower()
        assert "feedback_bind_one.md" in kwargs["body"]
        assert "feedback_bind_two.md" in kwargs["body"]
        assert "needs-triage" in kwargs["labels"]

    def test_existing_open_reconciliation_issue_is_reused(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.search_open_issues.return_value = [
            {
                "html_url": "https://github.com/souliane/teatree/issues/55",
                "body": "<!-- dream-binding-reconcile feedback_bind_one+feedback_bind_two -->",
            }
        ]
        outcomes = file_binding_reconciliation_tickets(host, repo="souliane/teatree", conflicts=[_conflict()])
        assert outcomes[0].filed is False
        assert outcomes[0].ticket_url == "https://github.com/souliane/teatree/issues/55"
        host.create_issue.assert_not_called()

    def test_dedup_key_is_order_independent(self) -> None:
        # The same pair surfaced with survivor/absorbed swapped dedups to one issue.
        host = MagicMock(spec=CodeHostBackend)
        host.search_open_issues.return_value = [
            {
                "html_url": "https://github.com/souliane/teatree/issues/55",
                "body": "<!-- dream-binding-reconcile feedback_bind_one+feedback_bind_two -->",
            }
        ]
        outcomes = file_binding_reconciliation_tickets(
            host,
            repo="souliane/teatree",
            conflicts=[_conflict(survivor="feedback_bind_two", absorbed="feedback_bind_one")],
        )
        assert outcomes[0].filed is False
        host.create_issue.assert_not_called()

    def test_banned_term_body_is_withheld(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        host = _fake_host()
        with patch("teatree.loops.dream.promote_memory.banned_terms_scanner.scan_text", return_value="customer-name"):
            outcomes = file_binding_reconciliation_tickets(host, repo="souliane/teatree", conflicts=[_conflict()])
        assert outcomes[0].withheld is True
        host.create_issue.assert_not_called()

    def test_bare_reference_body_is_withheld(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        host = _fake_host()
        with patch("teatree.loops.dream.promote_memory.find_bare_references", return_value=["#1234"]):
            outcomes = file_binding_reconciliation_tickets(host, repo="souliane/teatree", conflicts=[_conflict()])
        assert outcomes[0].withheld is True
        assert "bare reference" in (outcomes[0].reason or "")
        host.create_issue.assert_not_called()

    def test_dry_run_files_nothing(self) -> None:
        host = _fake_host()
        outcomes = file_binding_reconciliation_tickets(
            host, repo="souliane/teatree", conflicts=[_conflict()], dry_run=True
        )
        assert outcomes == []
        host.create_issue.assert_not_called()

    def test_search_hiccup_does_not_block_filing(self) -> None:
        # A search error must not block filing — the issue is filed anyway (refile-once
        # self-corrects on the next pass).
        host = MagicMock(spec=CodeHostBackend)
        host.search_open_issues.side_effect = RuntimeError("forge search down")
        host.create_issue.return_value = {"html_url": "https://github.com/souliane/teatree/issues/9001"}
        outcomes = file_binding_reconciliation_tickets(host, repo="souliane/teatree", conflicts=[_conflict()])
        assert outcomes[0].filed is True
        host.create_issue.assert_called_once()


class RetireResolvedMemoriesTestCase(TestCase):
    """A TICKETED row whose linked ticket is closed is retired (prose archived)."""

    def test_closed_ticket_retires_the_memory(self) -> None:
        row = _row(destination="skills/ship/SKILL.md")
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        host = MagicMock(spec=CodeHostBackend)
        host.get_issue.return_value = {"state": "closed"}
        retired = retire_resolved_memories(host)
        assert len(retired) == 1
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED

    def test_open_ticket_keeps_the_memory(self) -> None:
        row = _row(destination="skills/ship/SKILL.md")
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        host = MagicMock(spec=CodeHostBackend)
        host.get_issue.return_value = {"state": "open"}
        retired = retire_resolved_memories(host)
        assert retired == []
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED

    def test_binding_row_is_never_retired(self) -> None:
        row = _row(destination="skills/ship/SKILL.md", binding=True)
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        host = MagicMock(spec=CodeHostBackend)
        host.get_issue.return_value = {"state": "closed"}
        retired = retire_resolved_memories(host)
        # BINDING feedback is load-bearing user doctrine — never silently dropped.
        assert retired == []
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED

    def test_unresolvable_issue_state_keeps_the_memory(self) -> None:
        row = _row(destination="skills/ship/SKILL.md")
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        host = MagicMock(spec=CodeHostBackend)
        host.get_issue.side_effect = RuntimeError("forge down")
        retired = retire_resolved_memories(host)
        # A forge error must not retire a memory whose fix may not have landed.
        assert retired == []
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED

    def test_injected_is_resolved_predicate_drives_retirement(self) -> None:
        # The umbrella reconcile path retires off the gap-fix Ticket's authoritative
        # MERGED state, not a fragile forge re-read — via an injected predicate.
        row = _row(destination="skills/ship/SKILL.md")
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/pull/9100")
        host = MagicMock(spec=CodeHostBackend)
        host.get_issue.side_effect = AssertionError("the injected predicate must not round-trip the forge")
        retired = retire_resolved_memories(host, is_resolved=lambda _row: True)
        assert len(retired) == 1
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED


@patch.dict(os.environ, {"T3_DREAM_MEMORY_PROMOTE": "1"})
class RetroFindingSharesTheRetirementPathTestCase(TestCase):
    """A retro-recorded finding retires through the SAME drain, not a forked one.

    Retro no longer persists a lesson as a memory file: it records the finding in
    this ledger and promotes it onto the umbrella. The proof that this is reuse
    rather than a parallel mechanism is the end of the line — the row retires
    through ``retire_resolved_memories`` when its fix Ticket reaches MERGED,
    driven by the umbrella reconciler, with no retro-specific code in the path.
    """

    def test_a_retro_finding_retires_when_its_fix_merges(self) -> None:
        rule = "Run the tree-wide health gate before any push."
        row = record_finding(
            rule=rule, citation="pushed without running the gate, CI went red", destination="skills/ship/SKILL.md"
        )
        promote_finding(_fake_host(), rule=rule, umbrella_url=UMBRELLA)

        ticket = Ticket.objects.get(extra__dream_gap_batch__0__cluster_key=row.cluster_key)
        ticket.merge_extra(set_keys={"dream_gap_claimed_delivered": [row.cluster_key]})
        ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9100", repo="souliane/teatree", iid="9100", state="merged"
        )
        ticket.state = Ticket.State.MERGED
        ticket.save()

        body = f"## Open gaps\n- [ ] Workflow gap <!-- dream-gap {row.cluster_key} -->\n"
        host = _fake_host(body=body)
        host.get_issue.return_value = {"body": body, "state": "merged"}

        assert len(bp_module.reconcile_batches(host, umbrella_url=UMBRELLA)) == 1
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED


class DeleteSourceMemoryFilesTestCase(TestCase):
    """Retirement DELETES a promoted gap's source memory file(s) — 'memory tends to zero' (#4776)."""

    def test_a_memory_dir_source_file_is_deleted_and_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "project" / "memory"
            memory_dir.mkdir(parents=True)
            source = memory_dir / "feedback_run_gate.md"
            source.write_text("the lesson")
            row = _row(source_files=[str(source)])
            with patch("teatree.memory_audit.discover_memory_dirs", return_value=[memory_dir]):
                assert delete_source_memory_files(row) is True
            assert not source.exists()

    def test_a_non_memory_source_is_never_touched(self) -> None:
        # A row's source_files may carry a non-memory reference (e.g. a transcript
        # path) — only a memory-dir .md candidate is ever a delete target.
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "project" / "memory"
            memory_dir.mkdir(parents=True)
            outside = Path(tmp) / "sessions" / "session-a.jsonl"
            outside.parent.mkdir(parents=True)
            outside.write_text("transcript")
            row = _row(source_files=[str(outside)])
            with patch("teatree.memory_audit.discover_memory_dirs", return_value=[memory_dir]):
                assert delete_source_memory_files(row) is True
            assert outside.exists()

    def test_no_discovered_memory_dirs_is_vacuously_confirmed(self) -> None:
        row = _row(source_files=["feedback_run_gate.md"])
        with patch("teatree.memory_audit.discover_memory_dirs", return_value=[]):
            assert delete_source_memory_files(row) is True

    def test_an_unconfirmable_delete_is_reported_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "project" / "memory"
            memory_dir.mkdir(parents=True)
            source = memory_dir / "feedback_run_gate.md"
            source.write_text("the lesson")
            row = _row(source_files=[str(source)])
            with (
                patch("teatree.memory_audit.discover_memory_dirs", return_value=[memory_dir]),
                patch("pathlib.Path.unlink", side_effect=OSError("permission denied")),
            ):
                assert delete_source_memory_files(row) is False
            assert source.exists()

    def test_retire_deletes_the_file_before_retiring_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "project" / "memory"
            memory_dir.mkdir(parents=True)
            source = memory_dir / "feedback_run_gate.md"
            source.write_text("the lesson")
            row = _row(source_files=[str(source)])
            row.classify_core_gap()
            row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
            host = MagicMock(spec=CodeHostBackend)
            host.get_issue.return_value = {"state": "closed"}

            with patch("teatree.memory_audit.discover_memory_dirs", return_value=[memory_dir]):
                retired = retire_resolved_memories(host)

            assert len(retired) == 1
            assert not source.exists()
            row.refresh_from_db()
            assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED

    def test_retire_defers_the_row_when_the_file_cannot_be_confirmed_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "project" / "memory"
            memory_dir.mkdir(parents=True)
            source = memory_dir / "feedback_run_gate.md"
            source.write_text("the lesson")
            row = _row(source_files=[str(source)])
            row.classify_core_gap()
            row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
            host = MagicMock(spec=CodeHostBackend)
            host.get_issue.return_value = {"state": "closed"}

            with (
                patch("teatree.memory_audit.discover_memory_dirs", return_value=[memory_dir]),
                patch("pathlib.Path.unlink", side_effect=OSError("permission denied")),
            ):
                retired = retire_resolved_memories(host)

            assert retired == []
            row.refresh_from_db()
            assert row.disposition == ConsolidatedMemory.Disposition.TICKETED

    def test_binding_row_source_file_is_never_deleted(self) -> None:
        # BINDING is filtered out before delete_source_memory_files is ever reached —
        # the exemption is permanent, hand-flagged doctrine.
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "project" / "memory"
            memory_dir.mkdir(parents=True)
            source = memory_dir / "feedback_binding.md"
            source.write_text("binding doctrine")
            row = _row(source_files=[str(source)], binding=True)
            row.classify_core_gap()
            row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
            host = MagicMock(spec=CodeHostBackend)
            host.get_issue.return_value = {"state": "closed"}

            with patch("teatree.memory_audit.discover_memory_dirs", return_value=[memory_dir]):
                retired = retire_resolved_memories(host)

            assert retired == []
            assert source.exists()
            row.refresh_from_db()
            assert row.disposition == ConsolidatedMemory.Disposition.TICKETED


class UngroundedDestinationTestCase(TestCase):
    """A destination naming no place in the core tree is never promoted to a fix (#2663).

    The pre-fix classifier matched a prefix, so an invented package scheduled a coding
    task for a fix that had nowhere to land.
    """

    GHOST = "src/teatree/ghost_pkg/ghost.py"

    def test_a_ghost_destination_is_kept_as_memory_not_ticketed(self) -> None:
        row = _row(destination=self.GHOST)
        batch = PromotionBatch()

        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)

        assert outcomes == []
        assert batch.pending == []
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.USER_SPECIFIC_KEEP

    def test_a_stranded_ghost_row_is_held_at_the_promotion_chokepoint(self) -> None:
        # The needs_ticket() drain promotes rows a PRIOR pass classified WITHOUT
        # re-running the classifier, so triage alone would let already-recorded ghosts
        # through and the fix would never reach the live backlog.
        row = _row(destination=self.GHOST)
        row.classify_core_gap()
        batch = PromotionBatch()

        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)

        assert len(outcomes) == 1
        assert outcomes[0].filed is False
        assert outcomes[0].withheld is True
        assert "ghost_pkg" in outcomes[0].reason
        assert batch.pending == []

    def test_a_grounded_destination_outside_the_legacy_prefixes_is_still_promoted(self) -> None:
        _row(destination="evals/scenarios/rules.yaml")
        batch = PromotionBatch()

        outcomes = file_core_gap_tickets(umbrella_url=UMBRELLA, batch=batch)

        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        assert len(batch.pending) == 1
