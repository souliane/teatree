from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from teatree.core.backend_protocols import HEAD_SHA_UNREADABLE, CodeHostBackend, PrMergeState
from teatree.core.models import MergeAudit, MergeClear
from teatree.loop.scanners.base import PartialScanError, ScanSignal
from teatree.loop.scanners.unaudited_merge import UnauditedMergeScanner
from teatree.loop.tick import TickReport, TickRequest, run_tick
from teatree.types import ScannerError
from tests._file_backed_db import file_backed_default_database

SLUG = "acme/widget"
HEAD = "a" * 40
MERGED = "b" * 40
NOW = datetime(2026, 10, 25, 12, 5, tzinfo=UTC)


@dataclass
class _Merge:
    pr_id: int = 17
    sha: str = MERGED
    merged_at: datetime = NOW - timedelta(minutes=5)
    merged_by: str = "operator"
    head: str = HEAD
    state: str = "MERGED"
    reported_at: str = ""


@dataclass
class _Host:
    merges: list[_Merge] = field(default_factory=lambda: [_Merge()])
    merge_reads: list[int] = field(default_factory=list)
    listing_error: str = ""
    read_error: str = ""

    def list_merged_prs_since(self, *, repo: str, since: str) -> list[dict[str, object]]:
        assert repo == SLUG
        if self.listing_error:
            raise RuntimeError(self.listing_error)
        floor = datetime.fromisoformat(since)
        return [
            {"number": merge.pr_id, "html_url": f"https://github.com/{SLUG}/pull/{merge.pr_id}"}
            for merge in self.merges
            if merge.merged_at >= floor
        ]

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        self.merge_reads.append(pr_id)
        if self.read_error:
            raise RuntimeError(self.read_error)
        merge = next(candidate for candidate in self.merges if candidate.pr_id == pr_id)
        return PrMergeState(
            state=merge.state,
            merge_commit_oid=merge.sha,
            merged_by=merge.merged_by,
            merged_at=merge.reported_at or merge.merged_at.isoformat(),
        )

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        return next(candidate for candidate in self.merges if candidate.pr_id == pr_id).head


def _scanner(host: _Host, *, solo: bool = True, identities: tuple[str, ...] = ("owner",)) -> UnauditedMergeScanner:
    return UnauditedMergeScanner(
        host=cast("CodeHostBackend", host),
        repos=(SLUG,),
        overlay="example",
        identities=identities,
        solo_overlay=solo,
    )


def _audit(*, sha: str = MERGED, repo_slug: str = SLUG, pr_id: int = 17) -> None:
    clear = MergeClear.objects.create(
        pr_id=pr_id,
        slug=SLUG,
        reviewed_sha=HEAD,
        reviewer_identity="cold-reviewer",
        gh_verify_result="green",
        blast_class="docs",
    )
    MergeAudit.objects.create(clear=clear, merged_sha=sha, required_checks_status="green", repo_slug=repo_slug)


class TestUnauditedMergeScanner(TestCase):
    def _scan(self, host: _Host, *, solo: bool = True) -> list[ScanSignal]:
        return _scanner(host, solo=solo).scan(now=NOW)

    def test_hand_merge_without_audit_raises_one_alert(self) -> None:
        """Revert probe: treat every merge as audited and this goes red."""
        host = _Host()
        signals = self._scan(host)
        assert len(signals) == 1
        assert signals[0].kind == "merge.unaudited"
        assert signals[0].payload["merged_sha"] == MERGED
        assert signals[0].payload["merged_by"] == "operator"
        assert signals[0].payload["merged_at"] == (NOW - timedelta(minutes=5)).isoformat()
        assert signals[0].payload["independent_review_at_head"] is False
        assert signals[0].payload["hold_at_head"] is False
        assert host.merge_reads == [17]

    def test_independent_review_and_hold_are_both_reported(self) -> None:
        review = SimpleNamespace(standing_merge_safe=(17, "reviewer"), held_verdicts=((18, "reviewer"),))
        with patch("teatree.loop.scanners.unaudited_merge.head_review_state", return_value=review):
            signal = self._scan(_Host())[0]
        assert signal.payload["independent_review_at_head"] is True
        assert signal.payload["hold_at_head"] is True

    def test_keystone_audited_merge_raises_nothing_and_costs_no_forge_read(self) -> None:
        """Mutant: drop the per-PR audit skip and the merge-state read happens."""
        _audit()
        host = _Host()
        assert self._scan(host) == []
        assert host.merge_reads == []

    def test_an_audit_without_a_repo_slug_hides_the_merge_by_sha_after_one_read(self) -> None:
        """Mutant: key the decision on the PR tie alone and a legacy blank-slug audit raises a false alert."""
        _audit(repo_slug="")
        host = _Host()
        assert self._scan(host) == []
        assert host.merge_reads == [17]

    def test_an_audit_for_another_merge_does_not_hide_this_one(self) -> None:
        _audit(sha="c" * 40, repo_slug="", pr_id=99)
        assert self._scan(_Host())[0].kind == "merge.unaudited"

    def test_a_merge_younger_than_the_record_grace_raises_nothing(self) -> None:
        """Mutant: no grace and a sanctioned merge scanned before its audit lands raises a false DM."""
        inside = _Host([_Merge(merged_at=NOW - timedelta(minutes=2))])
        outside = _Host([_Merge(merged_at=NOW - timedelta(minutes=4))])
        assert self._scan(inside) == []
        assert len(self._scan(outside)) == 1

    def test_only_merges_inside_the_48_hour_window_are_read(self) -> None:
        """Mutant: a wider window lists the 49-hour-old merge and raises a second alert."""
        host = _Host(
            [
                _Merge(pr_id=1, sha="1" * 40, merged_at=NOW - timedelta(hours=47)),
                _Merge(pr_id=2, sha="2" * 40, merged_at=NOW - timedelta(hours=49)),
            ]
        )
        assert [signal.payload["pr_id"] for signal in self._scan(host)] == [1]
        assert host.merge_reads == [1]

    def test_the_scope_rule_alerts_only_for_solo_overlays_or_owner_identities(self) -> None:
        cases = {
            "colleague-on-collaborative": (False, "colleague", 0),
            "unknown-on-collaborative": (False, "", 0),
            "owner-on-collaborative": (False, "Owner", 1),
            "any-on-solo": (True, "colleague", 1),
        }
        for label, (solo, merged_by, alerts) in cases.items():
            with self.subTest(label):
                assert len(self._scan(_Host([_Merge(merged_by=merged_by)]), solo=solo)) == alerts

    def test_an_unreadable_merge_fails_loud(self) -> None:
        cases = {
            "not-merged-state": _Host([_Merge(state="OPEN")]),
            "empty-merge-sha": _Host([_Merge(sha="")]),
            "unreadable-head": _Host([_Merge(head=HEAD_SHA_UNREADABLE)]),
            "empty-head": _Host([_Merge(head="")]),
            "unparsable-merge-time": _Host([_Merge(reported_at="yesterday")]),
            "merge-state-read-raises": _Host(read_error="boom"),
        }
        for label, host in cases.items():
            with self.subTest(label), pytest.raises(ScannerError, match="widget#17"):
                self._scan(host)

    def test_forge_listing_failure_fails_loud(self) -> None:
        with pytest.raises(ScannerError, match="forge unavailable"):
            self._scan(_Host(listing_error="forge unavailable"))

    def test_one_unreadable_merge_keeps_the_alerts_collected_for_the_others(self) -> None:
        host = _Host([_Merge(pr_id=1, sha="1" * 40), _Merge(pr_id=2, sha=""), _Merge(pr_id=3, sha="3" * 40)])
        with pytest.raises(PartialScanError, match=r"widget#2") as raised:
            self._scan(host)
        assert [signal.payload["pr_id"] for signal in raised.value.signals] == [1, 3]


class TestUnauditedMergeTick(TransactionTestCase):
    def _tick(self, host: _Host) -> tuple[TickReport, MagicMock]:
        with (
            file_backed_default_database(),
            TemporaryDirectory() as temporary,
            patch("teatree.loop.unaudited_merge_alert.notify_with_fallback") as notify,
        ):
            report = run_tick(TickRequest(scanners=[_scanner(host)]), statusline_path=Path(temporary) / "statusline")
        return report, notify

    def test_one_tick_sends_one_owner_alert_for_a_hand_merge(self) -> None:
        host = _Host([_Merge(merged_at=timezone.now() - timedelta(hours=1))])
        report, notify = self._tick(host)
        assert report.errors == {}
        assert [signal.kind for signal in report.signals] == ["merge.unaudited"]
        notify.assert_called_once()

    def test_an_unreadable_merge_is_reported_beside_the_alert_for_the_readable_one(self) -> None:
        recent = timezone.now() - timedelta(hours=1)
        host = _Host(
            [_Merge(pr_id=1, sha="1" * 40, merged_at=recent), _Merge(pr_id=2, sha="", merged_at=recent)],
        )
        report, notify = self._tick(host)
        assert list(report.errors) == ["unaudited_merge"]
        assert [signal.payload["pr_id"] for signal in report.signals] == [1]
        notify.assert_called_once()
