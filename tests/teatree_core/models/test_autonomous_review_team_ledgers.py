"""Idempotency-ledger tests for the autonomous review-team pipeline (#1295).

Each ledger is keyed on a unique tuple and exposes a class-method
constructor that returns the new row on first observation and ``None``
on a duplicate. These tests pin both branches plus the missing-data
no-op and ``__str__`` rendering used by the admin/logs.
"""

import datetime as dt
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import AssessFinding, AssessSweepRun, RedMrFixAttempt, ScannedFailedE2E


class TestRedMrFixAttempt(TestCase):
    def test_claim_inserts_first_observation(self) -> None:
        row = RedMrFixAttempt.claim(
            pr_url="https://github.com/o/r/pull/1",
            head_sha="abc123def4567890",
            overlay="acme",
            worktree_hint="/wt/path",
        )

        assert row is not None
        assert row.pr_url == "https://github.com/o/r/pull/1"
        assert row.head_sha == "abc123def4567890"
        assert row.overlay == "acme"
        assert row.worktree_hint == "/wt/path"

    def test_claim_returns_none_on_duplicate(self) -> None:
        RedMrFixAttempt.claim(pr_url="https://github.com/o/r/pull/2", head_sha="sha1")
        again = RedMrFixAttempt.claim(pr_url="https://github.com/o/r/pull/2", head_sha="sha1")
        assert again is None

    def test_claim_no_op_on_missing_inputs(self) -> None:
        assert RedMrFixAttempt.claim(pr_url="", head_sha="sha1") is None
        assert RedMrFixAttempt.claim(pr_url="https://x/1", head_sha="") is None

    def test_claim_new_row_on_new_sha(self) -> None:
        first = RedMrFixAttempt.claim(pr_url="https://github.com/o/r/pull/3", head_sha="shaA")
        second = RedMrFixAttempt.claim(pr_url="https://github.com/o/r/pull/3", head_sha="shaB")
        assert first is not None
        assert second is not None
        assert first.pk != second.pk

    def test_str_renders_url_and_short_sha(self) -> None:
        row = RedMrFixAttempt.claim(pr_url="https://github.com/o/r/pull/4", head_sha="abcdef1234567890")
        assert row is not None
        rendered = str(row)
        assert "red-mr-fix" in rendered
        assert "abcdef12" in rendered


class TestScannedFailedE2E(TestCase):
    def test_record_inserts_first_observation(self) -> None:
        row = ScannedFailedE2E.record(
            channel="C123",
            slack_ts="1700000000.000100",
            spec_path="tests/e2e/spec.spec.ts",
            test_title="Login flow",
            overlay="acme",
        )

        assert row is not None
        assert row.channel == "C123"
        assert row.spec_path == "tests/e2e/spec.spec.ts"
        assert row.test_title == "Login flow"

    def test_record_returns_none_on_duplicate(self) -> None:
        ScannedFailedE2E.record(channel="C1", slack_ts="1.0", spec_path="a.ts")
        again = ScannedFailedE2E.record(channel="C1", slack_ts="1.0", spec_path="a.ts")
        assert again is None

    def test_record_no_op_on_missing_inputs(self) -> None:
        assert ScannedFailedE2E.record(channel="", slack_ts="1.0", spec_path="a.ts") is None
        assert ScannedFailedE2E.record(channel="C", slack_ts="", spec_path="a.ts") is None
        assert ScannedFailedE2E.record(channel="C", slack_ts="1.0", spec_path="") is None

    def test_record_new_row_on_different_spec(self) -> None:
        first = ScannedFailedE2E.record(channel="C2", slack_ts="2.0", spec_path="a.ts")
        second = ScannedFailedE2E.record(channel="C2", slack_ts="2.0", spec_path="b.ts")
        assert first is not None
        assert second is not None
        assert first.pk != second.pk

    def test_str_renders_channel_ts_spec(self) -> None:
        row = ScannedFailedE2E.record(channel="C9", slack_ts="9.0", spec_path="x.spec.ts")
        assert row is not None
        rendered = str(row)
        assert "failed-e2e" in rendered
        assert "C9/9.0" in rendered
        assert "x.spec.ts" in rendered


class TestAssessFinding(TestCase):
    def test_record_inserts_first_observation(self) -> None:
        row = AssessFinding.record(
            repo="/home/user/repos/skills",
            file_path="skills/code/SKILL.md",
            finding_fingerprint="hash-abc",
            severity="warning",
            finding_text="Section X is stale",
            overlay="acme",
        )

        assert row is not None
        assert row.repo == "/home/user/repos/skills"
        assert row.file_path == "skills/code/SKILL.md"
        assert row.finding_fingerprint == "hash-abc"
        assert row.severity == "warning"
        assert row.finding_text == "Section X is stale"
        assert row.overlay == "acme"

    def test_record_returns_none_on_duplicate(self) -> None:
        AssessFinding.record(repo="/r", file_path="f.md", finding_fingerprint="hash1")
        again = AssessFinding.record(repo="/r", file_path="f.md", finding_fingerprint="hash1")
        assert again is None

    def test_record_no_op_on_missing_inputs(self) -> None:
        assert AssessFinding.record(repo="", file_path="f.md", finding_fingerprint="hash") is None
        assert AssessFinding.record(repo="/r", file_path="", finding_fingerprint="hash") is None
        assert AssessFinding.record(repo="/r", file_path="f.md", finding_fingerprint="") is None

    def test_record_new_row_on_different_fingerprint(self) -> None:
        first = AssessFinding.record(repo="/r2", file_path="f.md", finding_fingerprint="A")
        second = AssessFinding.record(repo="/r2", file_path="f.md", finding_fingerprint="B")
        assert first is not None
        assert second is not None
        assert first.pk != second.pk

    def test_str_renders_repo_and_path(self) -> None:
        row = AssessFinding.record(repo="/home/u/repo", file_path="skills/x.md", finding_fingerprint="hash-z")
        assert row is not None
        rendered = str(row)
        assert "assess-finding" in rendered
        assert "/home/u/repo" in rendered
        assert "skills/x.md" in rendered


class TestAssessSweepRun(TestCase):
    def test_is_due_when_no_row_exists(self) -> None:
        assert AssessSweepRun.is_due(overlay="never-run", interval_hours=24) is True

    def test_is_due_when_last_run_old_enough(self) -> None:
        AssessSweepRun.mark_run(overlay="acme")
        row = AssessSweepRun.objects.get(overlay="acme")
        row.last_run_at = timezone.now() - timedelta(hours=48)
        row.save()

        assert AssessSweepRun.is_due(overlay="acme", interval_hours=24) is True

    def test_not_due_when_last_run_recent(self) -> None:
        AssessSweepRun.mark_run(overlay="acme-recent")
        assert AssessSweepRun.is_due(overlay="acme-recent", interval_hours=24) is False

    def test_mark_run_updates_existing_row(self) -> None:
        AssessSweepRun.mark_run(overlay="ovl")
        old = AssessSweepRun.objects.get(overlay="ovl")
        original_ts = old.last_run_at
        # Force the existing row backward to verify mark_run updates it.
        old.last_run_at = timezone.now() - timedelta(days=2)
        old.save()

        AssessSweepRun.mark_run(overlay="ovl")
        refreshed = AssessSweepRun.objects.get(overlay="ovl")
        assert refreshed.last_run_at >= original_ts - timedelta(seconds=1)

    def test_str_renders_overlay_and_timestamp(self) -> None:
        AssessSweepRun.mark_run(overlay="ovl-x")
        row = AssessSweepRun.objects.get(overlay="ovl-x")
        rendered = str(row)
        assert "assess-sweep" in rendered
        assert "ovl-x" in rendered

    def test_is_due_uses_local_now(self) -> None:
        """Spot-check that the fresh row counts as not due at interval=24."""
        AssessSweepRun.mark_run(overlay="ovl-now")
        # Negative interval means even a fresh row is due — exercises the
        # subtraction branch in is_due.
        assert AssessSweepRun.is_due(overlay="ovl-now", interval_hours=-1) is True
        # Sanity: a future timestamp is treated as not-due.
        row = AssessSweepRun.objects.get(overlay="ovl-now")
        row.last_run_at = dt.datetime.now(dt.UTC) + timedelta(hours=1)
        row.save()
        assert AssessSweepRun.is_due(overlay="ovl-now", interval_hours=24) is False
