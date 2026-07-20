"""Dream promote = fix-and-merge: the standing umbrella-issue ledger (#2663).

The promote/compliance phases no longer pile up ``needs-triage`` triage issues —
one per gap. Instead each grounded gap becomes a CHECKBOX item under the standing
umbrella issue (souliane/teatree#2663, reused daily, never closed), keyed on a
stable gap key so the same gap never double-adds, and its fix is SCHEDULED for a
coding agent via the existing ``Ticket.schedule_coding()`` path. When the fix
Ticket reaches MERGED, the checkbox is CHECKED and the linked ``ConsolidatedMemory``
is retired through the existing ``retire_resolved_memories``.

These tests drive that flow with an INJECTED fake code host and real ``Ticket`` /
``ConsolidatedMemory`` rows, so the whole flow is testable without an LLM or a
live forge. The umbrella body is plain markdown — a task-list whose lines each
carry an invisible ``<!-- dream-gap <key> -->`` marker for stable dedup.
"""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.ticket import Ticket
from teatree.loops.dream import umbrella_ledger as ul

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
REPO = "souliane/teatree"


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.get_issue.return_value = {"body": body}
    host.update_issue.return_value = {"number": 2663}
    return host


def _memory(*, key: str = "gap-1", binding: bool = False) -> ConsolidatedMemory:
    return ConsolidatedMemory.objects.create(
        cluster_key=key,
        rule="Run the tree-wide health gate before any push.",
        source_files=["feedback_run_gate.md"],
        durable_destination="skills/ship/SKILL.md",
        is_binding=binding,
        member_count=1,
        max_member_weight=90,
        verified_citation="pushed without running the gate, CI went red",
    )


class RenderCheckboxLineTestCase(TestCase):
    """A gap checkbox line carries its title and a stable invisible marker."""

    def test_unchecked_line_has_the_marker_and_title(self) -> None:
        line = ul.render_checkbox_line(gap_key="gap-1", title="Fix the gate", checked=False)
        assert line.startswith("- [ ] ")
        assert "Fix the gate" in line
        assert "<!-- dream-gap gap-1 -->" in line

    def test_checked_line_uses_an_x(self) -> None:
        line = ul.render_checkbox_line(gap_key="gap-1", title="Fix the gate", checked=True)
        assert line.startswith("- [x] ")

    def test_line_carries_the_ticket_link_when_given(self) -> None:
        line = ul.render_checkbox_line(
            gap_key="gap-1", title="Fix the gate", checked=False, ticket_url="https://example.com/pr/1"
        )
        assert "https://example.com/pr/1" in line


class UpsertGapCheckboxTestCase(TestCase):
    """A gap checkbox is added once and never double-added (deduped by gap key)."""

    def test_a_new_gap_appends_a_checkbox_and_rewrites_the_body(self) -> None:
        host = _fake_host(body="## Open gaps\n")
        added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate")
        assert added is True
        host.update_issue.assert_called_once()
        _, kwargs = host.update_issue.call_args
        assert "<!-- dream-gap gap-1 -->" in kwargs["body"]
        assert "Fix the gate" in kwargs["body"]

    def test_an_existing_gap_is_not_double_added(self) -> None:
        existing = "## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate")
        assert added is False
        # Idempotent: the body is unchanged, so no rewrite is issued.
        host.update_issue.assert_not_called()

    def test_a_second_distinct_gap_is_appended_alongside_the_first(self) -> None:
        existing = "## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-2", title="Fix the other")
        assert added is True
        _, kwargs = host.update_issue.call_args
        assert "<!-- dream-gap gap-1 -->" in kwargs["body"]
        assert "<!-- dream-gap gap-2 -->" in kwargs["body"]

    def test_unreadable_body_does_not_crash_and_files_nothing(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.get_issue.side_effect = RuntimeError("forge down")
        added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate")
        assert added is False
        host.update_issue.assert_not_called()


class CheckGapCheckboxTestCase(TestCase):
    """Checking a gap flips its line from unchecked to checked, idempotently."""

    def test_checking_flips_the_box(self) -> None:
        existing = "## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        checked = ul.check_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1")
        assert checked is True
        _, kwargs = host.update_issue.call_args
        assert "- [x] Fix the gate <!-- dream-gap gap-1 -->" in kwargs["body"]

    def test_checking_an_already_checked_box_is_a_noop(self) -> None:
        existing = "## Open gaps\n- [x] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        checked = ul.check_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1")
        assert checked is False
        host.update_issue.assert_not_called()

    def test_checking_an_absent_gap_is_a_noop(self) -> None:
        host = _fake_host(body="## Open gaps\n- [ ] Other <!-- dream-gap gap-9 -->\n")
        checked = ul.check_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1")
        assert checked is False
        host.update_issue.assert_not_called()


class ScheduleGapFixTestCase(TestCase):
    """A new gap schedules a headless coding task; an already-scheduled gap does not."""

    def test_new_gap_creates_an_author_ticket_and_schedules_coding(self) -> None:
        task = ul.schedule_gap_fix(umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate", cluster_key="gap-1")
        assert task is not None
        assert task.phase == "coding"
        ticket = task.ticket
        assert ticket.role == Ticket.Role.AUTHOR
        assert ticket.extra["dream_gap_key"] == "gap-1"
        assert ticket.extra["dream_memory_cluster_key"] == "gap-1"
        assert ticket.extra["dream_umbrella_url"] == UMBRELLA

    def test_already_scheduled_gap_is_not_rescheduled(self) -> None:
        first = ul.schedule_gap_fix(umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate", cluster_key="gap-1")
        second = ul.schedule_gap_fix(umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate", cluster_key="gap-1")
        assert first is not None
        assert second is None
        assert Ticket.objects.filter(extra__dream_gap_key="gap-1").count() == 1


class PromoteGapTestCase(TestCase):
    """The orchestration: a grounded gap upserts a checkbox AND schedules its fix."""

    def test_grounded_gap_upserts_checkbox_and_schedules_coding(self) -> None:
        host = _fake_host(body="## Open gaps\n")
        outcome = ul.promote_gap(
            host,
            umbrella_url=UMBRELLA,
            gap=ul.GapSpec(gap_key="gap-1", title="Fix the gate", cluster_key="gap-1"),
        )
        assert outcome.scheduled is True
        assert outcome.checkbox_added is True
        host.update_issue.assert_called_once()
        assert Ticket.objects.filter(extra__dream_gap_key="gap-1").exists()

    def test_dry_run_neither_edits_the_umbrella_nor_schedules(self) -> None:
        host = _fake_host(body="## Open gaps\n")
        outcome = ul.promote_gap(
            host,
            umbrella_url=UMBRELLA,
            gap=ul.GapSpec(gap_key="gap-1", title="Fix the gate", cluster_key="gap-1"),
            dry_run=True,
        )
        assert outcome.scheduled is False
        assert outcome.checkbox_added is False
        host.update_issue.assert_not_called()
        assert not Ticket.objects.filter(extra__dream_gap_key="gap-1").exists()

    def test_banned_term_title_is_withheld_not_filed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        host = _fake_host(body="## Open gaps\n")
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="customer-name"):
            outcome = ul.promote_gap(
                host,
                umbrella_url=UMBRELLA,
                gap=ul.GapSpec(gap_key="gap-1", title="Fix the gate", cluster_key="gap-1"),
            )
        assert outcome.withheld is True
        assert outcome.scheduled is False
        host.update_issue.assert_not_called()
        assert not Ticket.objects.filter(extra__dream_gap_key="gap-1").exists()

    def test_a_re_promoted_gap_neither_double_adds_nor_reschedules(self) -> None:
        existing = "## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        ul.schedule_gap_fix(umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate", cluster_key="gap-1")
        outcome = ul.promote_gap(
            host,
            umbrella_url=UMBRELLA,
            gap=ul.GapSpec(gap_key="gap-1", title="Fix the gate", cluster_key="gap-1"),
        )
        assert outcome.checkbox_added is False
        assert outcome.scheduled is False
        host.update_issue.assert_not_called()
        assert Ticket.objects.filter(extra__dream_gap_key="gap-1").count() == 1


class ReconcileMergedGapsTestCase(TestCase):
    """A merged gap-fix Ticket checks its checkbox and retires the linked memory."""

    def _scheduled_gap(self, *, key: str = "gap-1", binding: bool = False) -> Ticket:
        _memory(key=key, binding=binding)
        task = ul.schedule_gap_fix(umbrella_url=UMBRELLA, gap_key=key, title="Fix the gate", cluster_key=key)
        assert task is not None
        return task.ticket

    def test_merged_gap_checks_the_box_and_retires_the_memory(self) -> None:
        ticket = self._scheduled_gap()
        ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9100", repo=REPO, iid="9100", state="merged"
        )
        ticket.state = Ticket.State.MERGED
        ticket.save()
        existing = "## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        host.get_issue.return_value = {"body": existing, "state": "merged"}

        reconciled = ul.reconcile_merged_gaps(host, umbrella_url=UMBRELLA)

        assert len(reconciled) == 1
        # The checkbox is checked on the umbrella.
        update_bodies = [c.kwargs["body"] for c in host.update_issue.call_args_list]
        assert any("- [x] Fix the gate <!-- dream-gap gap-1 -->" in b for b in update_bodies)
        # The linked memory is retired through the existing retire path.
        memory = ConsolidatedMemory.objects.get(cluster_key="gap-1")
        assert memory.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED

    def test_reconciled_gap_is_stamped_and_not_re_read_next_pass(self) -> None:
        # F6.9: once a merged gap is reconciled (checkbox checked, memory retired) the
        # gap-fix ticket is STAMPED reconciled, so the next reconcile pass skips it
        # instead of re-reading the forge for the same merged gap forever.
        ticket = self._scheduled_gap()
        ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9100", repo=REPO, iid="9100", state="merged"
        )
        ticket.state = Ticket.State.MERGED
        ticket.save()
        existing = "## Open gaps\n- [x] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        host.get_issue.return_value = {"body": existing, "state": "merged"}

        first = ul.reconcile_merged_gaps(host, umbrella_url=UMBRELLA)
        assert len(first) == 1
        ticket.refresh_from_db()
        assert ticket.extra.get("dream_gap_reconciled_at")  # stamped reconciled

        # A second pass over the same merged gap does NOT touch it again.
        host2 = _fake_host(body=existing)
        host2.get_issue.return_value = {"body": existing, "state": "merged"}
        second = ul.reconcile_merged_gaps(host2, umbrella_url=UMBRELLA)
        assert second == []
        host2.get_issue.assert_not_called()  # no forge re-read for the already-reconciled gap

    def test_unmerged_gap_is_left_alone(self) -> None:
        self._scheduled_gap()
        host = _fake_host(body="## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n")
        reconciled = ul.reconcile_merged_gaps(host, umbrella_url=UMBRELLA)
        assert reconciled == []
        host.update_issue.assert_not_called()
        memory = ConsolidatedMemory.objects.get(cluster_key="gap-1")
        assert memory.disposition == ConsolidatedMemory.Disposition.UNTRIAGED

    def test_binding_memory_is_never_retired_even_when_its_gap_merges(self) -> None:
        ticket = self._scheduled_gap(binding=True)
        ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9100", repo=REPO, iid="9100", state="merged"
        )
        ticket.state = Ticket.State.MERGED
        ticket.save()
        existing = "## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)

        ul.reconcile_merged_gaps(host, umbrella_url=UMBRELLA)

        # BINDING feedback is load-bearing user doctrine — never silently dropped.
        memory = ConsolidatedMemory.objects.get(cluster_key="gap-1")
        assert memory.disposition != ConsolidatedMemory.Disposition.RESOLVED_RETIRED
