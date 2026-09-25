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

from unittest.mock import MagicMock, patch

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


class ElidedSnippetTestCase(TestCase):
    """A truncated snippet says so — a bare slice is indistinguishable from a complete rule."""

    def test_text_under_the_limit_is_returned_unchanged(self) -> None:
        assert ul.elided_snippet("short rule", 60) == "short rule"

    def test_text_exactly_at_the_limit_is_not_marked_truncated(self) -> None:
        text = "x" * 60
        assert ul.elided_snippet(text, 60) == text

    def test_text_over_the_limit_is_cut_and_marked_with_an_ellipsis(self) -> None:
        text = "x" * 70
        result = ul.elided_snippet(text, 60)
        assert result == "x" * 60 + "…"

    def test_never_silently_drops_text_with_no_truncation_signal(self) -> None:
        # The historical bug: a bare `text[:60]` reads identically whether the rule
        # was exactly 60 chars or 600 — the coder cannot tell truncation happened.
        result = ul.elided_snippet("a" * 100, 60)
        assert result.endswith("…")
        assert len(result) < 100


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


class GapPresentTestCase(TestCase):
    """Read-only discovery (#4176) — does this gap already ride the umbrella?"""

    def test_a_riding_gap_is_present_and_nothing_is_written(self) -> None:
        host = _fake_host(body="## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n")
        assert ul.gap_present(host, umbrella_url=UMBRELLA, gap_key="gap-1") is True
        host.update_issue.assert_not_called()

    def test_an_absent_gap_is_absent(self) -> None:
        assert ul.gap_present(_fake_host(), umbrella_url=UMBRELLA, gap_key="gap-1") is False

    def test_an_unreadable_body_is_unknown_never_absent(self) -> None:
        # None, not False: a caller must never conclude "absent" from a forge it could
        # not read, nor "present" — both are claims the read does not support.
        host = MagicMock(spec=CodeHostBackend)
        host.get_issue.side_effect = RuntimeError("forge down")
        assert ul.gap_present(host, umbrella_url=UMBRELLA, gap_key="gap-1") is None


class ReconcileMergedGapsTestCase(TestCase):
    """A merged gap-fix Ticket checks its checkbox and retires the linked memory.

    These tickets are the LEGACY per-gap scheme (#4776 replaced per-gap scheduling
    with batching for NEW gaps, but a ticket already in flight when that shipped
    keeps draining through this unchanged path) — constructed directly here since
    ``schedule_gap_fix`` (the function that used to mint one) is deleted.
    """

    def _scheduled_gap(self, *, key: str = "gap-1", binding: bool = False) -> Ticket:
        _memory(key=key, binding=binding)
        return Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-gap={key}",
            role=Ticket.Role.AUTHOR,
            short_description="Fix the gate",
            extra={"dream_gap_key": key, "dream_memory_cluster_key": key, "dream_umbrella_url": UMBRELLA},
        )

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

    def test_an_unreadable_umbrella_leaves_the_gap_unstamped_for_the_next_pass(self) -> None:
        # The reconciled stamp is permanent — it removes the ticket from every future
        # scan — so stamping on a forge read that never returned the body would leave the
        # umbrella showing an open box for a merged fix with nothing left to re-check it.
        ticket = self._scheduled_gap()
        ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9100", repo=REPO, iid="9100", state="merged"
        )
        ticket.state = Ticket.State.MERGED
        ticket.save()
        host = _fake_host()
        host.get_issue.side_effect = RuntimeError("forge 503")

        assert ul.reconcile_merged_gaps(host, umbrella_url=UMBRELLA) == []

        ticket.refresh_from_db()
        assert not (ticket.extra or {}).get("dream_gap_reconciled_at")
        assert ConsolidatedMemory.objects.get(cluster_key="gap-1").disposition != (
            ConsolidatedMemory.Disposition.RESOLVED_RETIRED
        )

    def test_a_refused_umbrella_write_leaves_the_gap_unstamped(self) -> None:
        ticket = self._scheduled_gap()
        ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9100", repo=REPO, iid="9100", state="merged"
        )
        ticket.state = Ticket.State.MERGED
        ticket.save()
        existing = "## Open gaps\n- [ ] Fix the gate <!-- dream-gap gap-1 -->\n"
        host = _fake_host(body=existing)
        host.get_issue.return_value = {"body": existing, "state": "merged"}

        with patch.object(ul, "_scrubbed_update", return_value=False):
            assert ul.reconcile_merged_gaps(host, umbrella_url=UMBRELLA) == []

        ticket.refresh_from_db()
        assert not (ticket.extra or {}).get("dream_gap_reconciled_at")

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


class ReconcileFoldedGapsTestCase(TestCase):
    """A gap folded into a host ticket is reconciled off the HOST's merge (#2663)."""

    def _scheduled_gap(self, *, key: str) -> Ticket:
        _memory(key=key)
        return Ticket.objects.create(
            issue_url=f"{UMBRELLA}#dream-gap={key}",
            role=Ticket.Role.AUTHOR,
            short_description="Fix the gate",
            extra={"dream_gap_key": key, "dream_memory_cluster_key": key, "dream_umbrella_url": UMBRELLA},
        )

    def _folded_member(self, *, into: int) -> Ticket:
        member = self._scheduled_gap(key="gap-member")
        member.state = Ticket.State.IGNORED
        member.save()
        member.merge_extra(set_keys={"dream_gap_folded_into": into})
        return member

    def _umbrella_body(self, *keys: str) -> str:
        lines = [f"- [ ] Fix the gate <!-- dream-gap {key} -->" for key in keys]
        return "## Open gaps\n" + "\n".join(lines) + "\n"

    def _host_reading(self, body: str) -> CodeHostBackend:
        host = _fake_host(body=body)
        host.get_issue.return_value = {"body": body, "state": "merged"}
        return host

    def test_a_folded_member_is_reconciled_when_its_host_merges(self) -> None:
        host_ticket = self._scheduled_gap(key="gap-host")
        host_ticket.pull_requests.create(
            url="https://github.com/souliane/teatree/pull/9200", repo=REPO, iid="9200", state="merged"
        )
        host_ticket.state = Ticket.State.MERGED
        host_ticket.save()
        member = self._folded_member(into=host_ticket.pk)
        forge = self._host_reading(self._umbrella_body("gap-host", "gap-member"))

        reconciled = ul.reconcile_merged_gaps(forge, umbrella_url=UMBRELLA)

        assert {ticket.pk for ticket in reconciled} == {host_ticket.pk, member.pk}
        bodies = [call.kwargs["body"] for call in forge.update_issue.call_args_list]
        assert any("- [x] Fix the gate <!-- dream-gap gap-member -->" in body for body in bodies)
        member.refresh_from_db()
        assert member.extra.get("dream_gap_reconciled_at")
        memory = ConsolidatedMemory.objects.get(cluster_key="gap-member")
        assert memory.disposition == ConsolidatedMemory.Disposition.RESOLVED_RETIRED

    def test_a_folded_member_whose_host_has_not_merged_is_left_alone(self) -> None:
        host_ticket = self._scheduled_gap(key="gap-host")
        member = self._folded_member(into=host_ticket.pk)
        forge = self._host_reading(self._umbrella_body("gap-member"))

        assert ul.reconcile_merged_gaps(forge, umbrella_url=UMBRELLA) == []

        forge.update_issue.assert_not_called()
        member.refresh_from_db()
        assert not (member.extra or {}).get("dream_gap_reconciled_at")

    def test_a_folded_member_pointing_at_no_ticket_is_left_alone(self) -> None:
        member = self._folded_member(into=9_999_999)
        forge = self._host_reading(self._umbrella_body("gap-member"))

        assert ul.reconcile_merged_gaps(forge, umbrella_url=UMBRELLA) == []

        forge.update_issue.assert_not_called()
        member.refresh_from_db()
        assert not (member.extra or {}).get("dream_gap_reconciled_at")
