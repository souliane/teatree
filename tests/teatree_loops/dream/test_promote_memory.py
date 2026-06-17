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

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream.promote_memory import (
    MemoryDisposition,
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


def _fake_host(*, created_url: str = "https://github.com/souliane/teatree/issues/9001") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.search_open_issues.return_value = []
    host.create_issue.return_value = {"html_url": created_url}
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
    """Core-gap rows get a deduped teatree ticket and advance to TICKETED."""

    def test_core_gap_row_files_a_ticket_and_records_the_url(self) -> None:
        row = _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        outcomes = file_core_gap_tickets(host, repo="souliane/teatree")
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert row.ticket_url == "https://github.com/souliane/teatree/issues/9001"
        host.create_issue.assert_called_once()

    def test_user_specific_row_is_classified_and_files_nothing(self) -> None:
        row = _row(destination="feedback/tone.md")
        host = _fake_host()
        file_core_gap_tickets(host, repo="souliane/teatree")
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.USER_SPECIFIC_KEEP
        host.create_issue.assert_not_called()

    def test_ticket_body_carries_the_cited_mistake(self) -> None:
        _row(destination="skills/ship/SKILL.md", citation="pushed without the gate, CI went red")
        host = _fake_host()
        file_core_gap_tickets(host, repo="souliane/teatree")
        _, kwargs = host.create_issue.call_args
        assert "pushed without the gate" in kwargs["body"]
        assert "needs-triage" in kwargs["labels"]

    def test_already_ticketed_row_is_not_refiled(self) -> None:
        row = _row(destination="skills/ship/SKILL.md")
        row.classify_core_gap()
        row.mark_ticketed("https://github.com/souliane/teatree/issues/42")
        host = _fake_host()
        outcomes = file_core_gap_tickets(host, repo="souliane/teatree")
        # The TICKETED row is no longer untriaged — nothing is refiled.
        assert outcomes == []
        host.create_issue.assert_not_called()

    def test_existing_open_issue_is_reused_not_duplicated(self) -> None:
        row = _row(key="dedup-key", destination="skills/ship/SKILL.md")
        host = MagicMock(spec=CodeHostBackend)
        host.search_open_issues.return_value = [
            {"html_url": "https://github.com/souliane/teatree/issues/77", "body": "<!-- dream-memory-gap dedup-key -->"}
        ]
        outcomes = file_core_gap_tickets(host, repo="souliane/teatree")
        assert outcomes[0].filed is False
        assert outcomes[0].ticket_url == "https://github.com/souliane/teatree/issues/77"
        row.refresh_from_db()
        assert row.disposition == ConsolidatedMemory.Disposition.TICKETED
        assert row.ticket_url == "https://github.com/souliane/teatree/issues/77"
        host.create_issue.assert_not_called()

    def test_banned_term_body_is_withheld_not_filed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        with patch("teatree.loops.dream.promote_memory.banned_terms_scanner.scan_text", return_value="customer-name"):
            outcomes = file_core_gap_tickets(host, repo="souliane/teatree")
        assert outcomes[0].filed is False
        assert outcomes[0].withheld is True
        host.create_issue.assert_not_called()

    def test_dry_run_classifies_but_files_nothing(self) -> None:
        row = _row(destination="skills/ship/SKILL.md")
        host = _fake_host()
        file_core_gap_tickets(host, repo="souliane/teatree", dry_run=True)
        row.refresh_from_db()
        # The classification still advances (cheap, reversible), but no ticket is filed.
        assert row.disposition == ConsolidatedMemory.Disposition.CORE_GAP_NEEDS_TICKET
        host.create_issue.assert_not_called()


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
