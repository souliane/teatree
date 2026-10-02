"""The ``0030`` migration normalizes reviewer identity, dedups, then constrains (F8).

Verified against a DB already at the PRIOR head (``0029``) carrying pre-existing
duplicate verdict rows — the 66%-duplicate reality — not just a fresh migrate:
the backfill fills ``reviewer_identity_normalized``, the dedup keeps the NEWEST
row per ``(slug, pr, sha, normalized-identity)``, and the unique constraint then
lands cleanly on the deduped table. Anti-vacuous: without the dedup ``RunPython``
the ``AddConstraint`` would fail on the duplicate rows below.
"""

import datetime as dt

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0029_dm_sweep_loop_and_directive_cadence")
_AFTER = ("core", "0030_review_verdict_reviewer_identity_normalized")

_SHA = "a" * 40
_T0 = dt.datetime(2026, 6, 28, 12, 0, 0, tzinfo=dt.UTC)


@pytest.mark.timeout(240)
class TestReviewVerdictNormalizeMigration(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_backfill_dedups_and_constrains_a_dup_laden_table(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        old_apps = executor.loader.project_state(_BEFORE).apps
        old_verdict = old_apps.get_model("core", "ReviewVerdict")

        # Three rows for ONE head under case/whitespace spellings of ONE reviewer
        # ("Codex") — the duplicate pile. The newest (merge_safe) must survive.
        old_verdict.objects.create(
            slug="souliane/teatree",
            pr_id=1,
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="Codex",
            blast_class="logic",
            gh_verify_result="failed",
            recorded_at=_T0,
        )
        old_verdict.objects.create(
            slug="souliane/teatree",
            pr_id=1,
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="codex ",
            blast_class="logic",
            gh_verify_result="failed",
            recorded_at=_T0 + dt.timedelta(seconds=1),
        )
        newest = old_verdict.objects.create(
            slug="souliane/teatree",
            pr_id=1,
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="  codex  ",
            blast_class="logic",
            gh_verify_result="green",
            recorded_at=_T0 + dt.timedelta(seconds=2),
        )
        # A genuinely distinct reviewer at the same head must survive untouched.
        distinct = old_verdict.objects.create(
            slug="souliane/teatree",
            pr_id=1,
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="reviewer-b",
            blast_class="logic",
            gh_verify_result="failed",
            recorded_at=_T0,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])
        new_apps = executor.loader.project_state(_AFTER).apps
        new_verdict = new_apps.get_model("core", "ReviewVerdict")

        surviving = list(new_verdict.objects.filter(slug="souliane/teatree", pr_id=1, reviewed_sha=_SHA))
        by_pk = {row.pk: row for row in surviving}
        # The three "codex" spellings collapse to the newest; reviewer-b stays.
        assert len(surviving) == 2
        assert newest.pk in by_pk
        assert distinct.pk in by_pk
        assert by_pk[newest.pk].reviewer_identity_normalized == "codex"
        assert by_pk[newest.pk].verdict == "merge_safe"
        assert by_pk[distinct.pk].reviewer_identity_normalized == "reviewer-b"
