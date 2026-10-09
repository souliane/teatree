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
from teatree.loops.dream import umbrella_ledger as ul
from teatree.loops.dream.umbrella_ledger import code_host_for
from tests.teatree_loops.dream._own_umbrella import SELF_LOGIN, claims_self

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
REPO = "souliane/teatree"


def _fake_host(*, body: str = "## Open gaps\n", author: str = SELF_LOGIN) -> CodeHostBackend:
    """The standing umbrella, as the owner filed it — the #162 Rule 5 read needs an author."""
    host = claims_self(MagicMock(spec=CodeHostBackend))
    host.get_issue.return_value = {"body": body, "user": {"login": author}}
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
        host = claims_self(MagicMock(spec=CodeHostBackend))
        host.get_issue.side_effect = RuntimeError("forge down")
        added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate")
        assert added is False
        host.update_issue.assert_not_called()

    def test_an_umbrella_someone_else_filed_is_left_untouched(self) -> None:
        """#162 Rule 5 — the body we compute an upsert from is the body we were authorised on."""
        host = _fake_host(author="someone.else")
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
        host = claims_self(MagicMock(spec=CodeHostBackend))
        host.get_issue.side_effect = RuntimeError("forge down")
        assert ul.gap_present(host, umbrella_url=UMBRELLA, gap_key="gap-1") is None


class PromotionAnchorTestCase(TestCase):
    """A TICKETED url is a promotion-time placeholder only when it carries a dream fragment."""

    def test_only_dream_fragments_are_promotion_anchors(self) -> None:
        cases = {
            f"{UMBRELLA}#dream-batch=abc123": True,
            UMBRELLA: False,
            "https://github.com/souliane/teatree/pull/9100": False,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                assert ul.is_promotion_anchor(url) is expected


class StampMemoryMergedTestCase(TestCase):
    """The merge stamp replaces a promotion anchor, never a real PR url."""

    PR_URL = "https://github.com/souliane/teatree/pull/9100"

    def test_a_promotion_anchor_is_restamped_with_the_merged_pr(self) -> None:
        row = _memory(key="gap-1")
        row.classify_core_gap()
        row.mark_ticketed(f"{UMBRELLA}#dream-batch=gap-1")

        assert ul._stamp_memory_merged("gap-1", merged_url=self.PR_URL) is True
        row.refresh_from_db()
        assert row.ticket_url == self.PR_URL

    def test_a_row_already_on_a_real_pr_is_left_alone(self) -> None:
        row = _memory()
        row.classify_core_gap()
        earlier = "https://github.com/souliane/teatree/pull/8000"
        row.mark_ticketed(earlier)

        assert ul._stamp_memory_merged("gap-1", merged_url=self.PR_URL) is False
        row.refresh_from_db()
        assert row.ticket_url == earlier


class TestCodeHostFor(TestCase):
    def test_the_owning_overlay_is_asked_first(self) -> None:
        owner, other = MagicMock(name="owner"), MagicMock(name="other")
        owner_host = MagicMock(spec=CodeHostBackend)
        with (
            patch("teatree.loops.dream.umbrella_ledger.get_all_overlays", return_value={"a": other, "b": owner}),
            patch("teatree.loops.dream.umbrella_ledger.infer_overlay_for_url", return_value="b"),
            patch(
                "teatree.loops.dream.umbrella_ledger.get_code_host_for_url",
                side_effect=lambda overlay, _url: owner_host if overlay is owner else MagicMock(),
            ),
        ):
            assert code_host_for(UMBRELLA) is owner_host

    def test_no_overlay_reaching_the_forge_is_none(self) -> None:
        with (
            patch("teatree.loops.dream.umbrella_ledger.get_all_overlays", return_value={"a": MagicMock()}),
            patch("teatree.loops.dream.umbrella_ledger.infer_overlay_for_url", return_value=""),
            patch("teatree.loops.dream.umbrella_ledger.get_code_host_for_url", return_value=None),
        ):
            assert code_host_for(UMBRELLA) is None
