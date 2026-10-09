"""``t3 <overlay> ticket merge`` hands ``--no-squash`` to the keystone, and squashes by default."""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import MergeClear

_MERGE_TICKET_PR = "teatree.core.management.commands._merge_keystone_commands.merge_ticket_pr"


def _clear() -> MergeClear:
    return MergeClear.objects.create(
        pr_id=7,
        slug="souliane/teatree",
        reviewed_sha="a" * 40,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


class TestNoSquashFlag(TestCase):
    def test_no_squash_reaches_the_keystone(self) -> None:
        clear = _clear()
        with patch(_MERGE_TICKET_PR) as merge_pr:
            cast("dict", call_command("ticket", "merge", str(clear.pk), no_squash=True))

        assert merge_pr.call_args.kwargs["squash"] is False

    def test_the_keystone_squashes_by_default(self) -> None:
        clear = _clear()
        with patch(_MERGE_TICKET_PR) as merge_pr:
            call_command("ticket", "merge", str(clear.pk))

        assert merge_pr.call_args.kwargs["squash"] is True
