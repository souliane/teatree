"""End-to-end integration test for the ``loop_self_improve`` mgmt command.

Drives the full path: seed a smell, ``call_command('loop_self_improve',
tier='cheap')``, then assert a ``SelfImproveFiring`` row is recorded for
at least one detector.
"""

import datetime as dt
import io
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import MergeClear, SelfImproveFiring
from teatree.core.models.merge_clear import ClearRequest


class LoopSelfImproveCommandTests(TestCase):
    """End-to-end coverage for the ``loop_self_improve`` mgmt command.

    Pin the RAM probe to a deterministic low value so the budget gate
    never skips the cycle just because the host happens to be loaded —
    same seam used by :mod:`tests.teatree_loop.self_improve.test_budget`.
    """

    def setUp(self) -> None:
        super().setUp()
        self._ram_patch = patch(
            "teatree.loop.self_improve.budget._read_ram_used_percent",
            return_value=10.0,
        )
        self._ram_patch.start()
        self.addCleanup(self._ram_patch.stop)

    def test_command_writes_firing_row_for_seeded_smell(self) -> None:
        # Seed a forgotten-merge smell: CLEAR > 30 min old, no audit.
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=999,
                slug="souliane/teatree",
                reviewed_sha="deadbeefcafe1234" + "0" * 24,
                reviewer_identity="reviewer@example.com",
                gh_verify_result="green",
                blast_class="logic",
            )
        )
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)

        out = io.StringIO()
        call_command("loop_self_improve", tier="cheap", stdout=out)

        # The forgotten_merge detector must have written a firing.
        assert SelfImproveFiring.objects.filter(detector="forgotten_merge").count() == 1
        # And the human summary mentions the cycle ran.
        assert "OK" in out.getvalue() or "SKIP" in out.getvalue()

    def test_command_json_output_includes_reports(self) -> None:
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=1000,
                slug="souliane/teatree",
                reviewed_sha="deadbeefcafe5678" + "0" * 24,
                reviewer_identity="reviewer@example.com",
                gh_verify_result="green",
                blast_class="logic",
            )
        )
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)

        out = io.StringIO()
        call_command("loop_self_improve", tier="cheap", json_output=True, stdout=out)
        import json  # noqa: PLC0415

        payload = json.loads(out.getvalue())
        assert payload["tier"] == "cheap"
        # Either the cycle ran (report_count ≥ 1) or it skipped — both
        # outcomes have the contract keys.
        assert "report_count" in payload
        assert "action_count" in payload
