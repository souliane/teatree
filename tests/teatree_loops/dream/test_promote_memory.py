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

from pathlib import Path
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream.merge import BindingConflict
from teatree.loops.dream.promote_memory import (
    MemoryDisposition,
    file_binding_reconciliation_tickets,
    file_core_gap_tickets,
    retire_resolved_memories,
    triage_disposition,
)


def _row(
    *,
    key: str = "k1",
    rule: str = "Run the tree-wide health gate before any push.",
    destination: str = "skills/ship/SKILL.md",
    binding: bool = False,
    citation: str = "pushed without running the gate, CI went red",
) -> ConsolidatedMemory:
    return ConsolidatedMemory.objects.create(
        cluster_key=key,
        rule=rule,
        source_files=["feedback_run_gate.md"],
        durable_destination=destination,
        is_binding=binding,
        member_count=1,
        max_member_weight=90,
        verified_citation=citation,
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


class FileCoreGapTicketsTestCase(TestCase):
    """Core-gap rows upsert an umbrella checkbox + schedule a fix — never a triage issue."""

    def test_core_gap_row_upserts_a_checkbox_and_schedules_a_fix(self) -> None:
        from teatree.core.models.task import Task  # noqa: PLC0415
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        row = _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        outcomes = file_core_gap_tickets(host, umbrella_url=UMBRELLA)
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        # No fresh needs-triage issue is filed — the gap rides the umbrella + a coding task.
        host.create_issue.assert_not_called()
        host.update_issue.assert_called_once()
        assert Ticket.objects.filter(extra__dream_gap_key="k1").exists()
        assert Task.objects.filter(phase="coding").exists()
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET

    def test_user_specific_row_is_classified_and_files_nothing(self) -> None:
        row = _row(destination="feedback/tone.md")
        host = _fake_host()
        file_core_gap_tickets(host, umbrella_url=UMBRELLA)
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.USER_SPECIFIC_KEEP
        host.create_issue.assert_not_called()
        host.update_issue.assert_not_called()

    def test_checkbox_carries_the_gap_marker(self) -> None:
        _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        file_core_gap_tickets(host, umbrella_url=UMBRELLA)
        _, kwargs = host.update_issue.call_args
        assert "<!-- dream-gap k1 -->" in kwargs["body"]

    def test_already_scheduled_gap_is_not_double_added(self) -> None:
        existing = "## Open gaps\n- [ ] Workflow gap (dreaming Pass 2): Run the tree-wide ... <!-- dream-gap k1 -->\n"
        _row(destination="skills/ship/SKILL.md")
        host = _fake_host(body=existing)
        file_core_gap_tickets(host, umbrella_url=UMBRELLA)
        # The checkbox is already present — no rewrite.
        host.update_issue.assert_not_called()

    def test_banned_term_title_is_withheld_not_promoted(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="customer-name"):
            outcomes = file_core_gap_tickets(host, umbrella_url=UMBRELLA)
        assert outcomes[0].filed is False
        assert outcomes[0].withheld is True
        host.update_issue.assert_not_called()
        assert not Ticket.objects.filter(extra__dream_gap_key="k1").exists()

    def test_dry_run_classifies_but_neither_edits_nor_schedules(self) -> None:
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        row = _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        file_core_gap_tickets(host, umbrella_url=UMBRELLA, dry_run=True)
        row.refresh_from_db()
        # The classification still advances (cheap, reversible), but nothing is promoted.
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET
        host.update_issue.assert_not_called()
        assert not Ticket.objects.filter(extra__dream_gap_key="k1").exists()


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
        retired = retire_resolved_memories(host, is_resolved=lambda _url: True)
        assert len(retired) == 1
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED
