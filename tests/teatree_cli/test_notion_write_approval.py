"""The recorded approval a Notion write spends: bound to the exact text, single-use, never inferred from posture."""

import pytest
from django.test import TestCase

from teatree.backends.notion.errors import NotionBlockChangedError, NotionWriteNotApprovedError
from teatree.cli.notion_write_approval import approval_hint, spent_approval, write_target
from teatree.core.models import OnBehalfApproval, OnBehalfAudit

_OWNER = "alice.example"
_ACTION = "notion_replace"
_OBJECT = "page-1#block-1"


def _a_write_that_fails(target: str) -> None:
    with spent_approval(target, _ACTION):
        msg = "boom"
        raise RuntimeError(msg)


class TestWriteTarget:
    def test_the_target_names_the_object_and_a_full_length_digest_of_what_is_bound(self) -> None:
        target = write_target(_OBJECT, "before", "old", "new")

        scope, _, digest = target.rpartition(":")
        assert scope == f"notion:{_OBJECT}"
        assert len(digest) == 64

    @pytest.mark.parametrize("changed", [("BEFORE", "old", "new"), ("before", "OLD", "new"), ("before", "old", "NEW")])
    def test_changing_any_bound_part_changes_the_target(self, changed: tuple[str, ...]) -> None:
        assert write_target(_OBJECT, *changed) != write_target(_OBJECT, "before", "old", "new")

    def test_parts_that_concatenate_alike_do_not_collide(self) -> None:
        assert write_target(_OBJECT, "ab", "c") != write_target(_OBJECT, "a", "bc")

    def test_the_hint_carries_the_command_that_records_the_approval(self) -> None:
        hint = approval_hint(write_target(_OBJECT, "x"), _ACTION)

        assert hint["record_command"] == (
            f"t3 review approve-on-behalf '{hint['target']}' {_ACTION} --approver <owner-id>"
        )


class TestSpentApproval(TestCase):
    def test_a_matching_recorded_approval_is_consumed_and_audited_once_the_block_exits(self) -> None:
        target = write_target(_OBJECT, "x")
        approval = OnBehalfApproval.record(target, _ACTION, _OWNER)

        with spent_approval(target, _ACTION):
            approval.refresh_from_db()
            assert approval.consumed_at is not None
            assert not OnBehalfAudit.objects.exists()

        assert OnBehalfAudit.objects.get().approval_id == approval.pk

    def test_the_audit_is_written_even_when_the_write_inside_fails(self) -> None:
        target = write_target(_OBJECT, "x")
        OnBehalfApproval.record(target, _ACTION, _OWNER)

        with pytest.raises(RuntimeError, match="boom"):
            _a_write_that_fails(target)

        assert OnBehalfAudit.objects.count() == 1

    def test_without_an_approval_the_block_never_runs(self) -> None:
        ran = []

        with (
            pytest.raises(NotionWriteNotApprovedError, match="approve-on-behalf"),
            spent_approval(write_target(_OBJECT, "x"), _ACTION),
        ):
            ran.append(True)

        assert ran == []
        assert not OnBehalfAudit.objects.exists()

    def test_an_approval_for_the_same_object_but_other_text_is_named_as_stale(self) -> None:
        OnBehalfApproval.record(write_target(_OBJECT, "before", "old", "new"), _ACTION, _OWNER)

        with (
            pytest.raises(NotionBlockChangedError, match="different text"),
            spent_approval(write_target(_OBJECT, "EDITED", "old", "new"), _ACTION),
        ):
            pass

    def test_an_approval_for_another_action_does_not_cover_this_one(self) -> None:
        target = write_target(_OBJECT, "x")
        OnBehalfApproval.record(target, "notion_comment", _OWNER)

        with pytest.raises(NotionWriteNotApprovedError), spent_approval(target, _ACTION):
            pass
