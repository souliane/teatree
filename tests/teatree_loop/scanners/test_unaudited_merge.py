from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from django.test import TestCase, TransactionTestCase

from teatree.core.backend_protocols import CodeHostBackend, PrMergeState
from teatree.core.models import MergeAudit, MergeClear
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.unaudited_merge import UnauditedMergeScanner
from teatree.loop.tick import TickRequest, run_tick
from teatree.types import ScannerError

SLUG = "acme/widget"
HEAD = "a" * 40
MERGED = "b" * 40
WHEN = "2026-10-25T12:00:00Z"


class _Host:
    def __init__(self) -> None:
        self.merge_reads = 0
        self.fail_listing = False

    def list_merged_prs_since(self, *, repo: str, since: str) -> list[dict[str, object]]:
        assert repo == SLUG
        if self.fail_listing:
            message = "forge unavailable"
            raise RuntimeError(message)
        return [{"number": 17, "html_url": f"https://github.com/{SLUG}/pull/17"}]

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        self.merge_reads += 1
        return PrMergeState(state="MERGED", merge_commit_oid=MERGED, merged_by="operator", merged_at=WHEN)

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        return HEAD


class TestUnauditedMergeScanner(TestCase):
    def _scan(self, host: _Host) -> list[ScanSignal]:
        scanner = UnauditedMergeScanner(host=cast("CodeHostBackend", host), repos=(SLUG,), overlay="example")
        return scanner.scan(now=datetime(2026, 10, 25, 12, 5, tzinfo=UTC))

    def test_hand_merge_without_audit_raises_one_alert(self) -> None:
        signals = self._scan(_Host())
        assert len(signals) == 1
        assert signals[0].kind == "merge.unaudited"
        assert signals[0].payload["merged_sha"] == MERGED
        assert signals[0].payload["merged_by"] == "operator"
        assert signals[0].payload["merged_at"] == WHEN
        assert signals[0].payload["independent_review_at_head"] is False
        assert signals[0].payload["hold_at_head"] is False

    def test_independent_review_and_hold_are_both_reported(self) -> None:
        review = SimpleNamespace(standing_merge_safe=(17, "reviewer"), held_verdicts=((18, "reviewer"),))
        with patch("teatree.loop.scanners.unaudited_merge.head_review_state", return_value=review):
            signal = self._scan(_Host())[0]
        assert signal.payload["independent_review_at_head"] is True
        assert signal.payload["hold_at_head"] is True

    def test_audited_merge_is_silent(self) -> None:
        clear = MergeClear.objects.create(
            pr_id=17,
            slug=SLUG,
            reviewed_sha=HEAD,
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
            blast_class="docs",
        )
        MergeAudit.objects.create(clear=clear, merged_sha=MERGED, required_checks_status="green", repo_slug=SLUG)
        host = _Host()
        assert self._scan(host) == []

    def test_an_audit_for_the_pr_at_another_sha_does_not_hide_the_merge(self) -> None:
        clear = MergeClear.objects.create(
            pr_id=17,
            slug=SLUG,
            reviewed_sha=HEAD,
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
            blast_class="docs",
        )
        MergeAudit.objects.create(clear=clear, merged_sha="c" * 40, required_checks_status="green", repo_slug=SLUG)
        assert self._scan(_Host())[0].kind == "merge.unaudited"

    def test_forge_listing_failure_fails_loud(self) -> None:
        host = _Host()
        host.fail_listing = True
        with pytest.raises(ScannerError, match="forge unavailable"):
            self._scan(host)


class TestUnauditedMergeTick(TransactionTestCase):
    def test_one_tick_sends_one_owner_alert_for_a_hand_merge(self) -> None:
        scanner = UnauditedMergeScanner(host=cast("CodeHostBackend", _Host()), repos=(SLUG,), overlay="example")
        with (
            TemporaryDirectory() as temporary,
            patch("teatree.loop.unaudited_merge_alert.notify_with_fallback") as notify,
        ):
            report = run_tick(TickRequest(scanners=[scanner]), statusline_path=Path(temporary) / "statusline")
        assert report.errors == {}
        assert [signal.kind for signal in report.signals] == ["merge.unaudited"]
        notify.assert_called_once()
