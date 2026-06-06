"""``t3 <overlay> review record`` triggers the pr_sweep merge on demand (#2017).

Reproduces the incident: a ``merge_safe`` :class:`ReviewVerdict` was recorded
for an own PR the periodic sweep was waiting on, but nothing re-ran the sweep —
so the autonomous merge idled a full ~12-min tick cadence and a parallel human
keystone-merged the PR first. The fix makes ``review record`` run the sweep's
single-PR decision the moment a ``merge_safe`` verdict lands, so the merge does
not depend on the next periodic tick.

Integration-style: the real ``review record`` management command and real ORM
rows; only the sweep-scanner builder (which would otherwise reach the forge over
``gh``) is stubbed with a fake whose single-PR evaluation is asserted.
"""

from dataclasses import dataclass, field
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.loop.scanners.pr_sweep import MergeAttempt

pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 2014
_HEAD = "497d468df76022b280caffceb400739d5ced9baa"
_SWEEP_MOD = "teatree.loop.sweep_on_demand"


@dataclass(slots=True)
class _FakeSweepScanner:
    """Stand-in for the per-overlay ``PrSweepScanner`` the trigger builds."""

    merged: bool = True
    calls: list[tuple[str, int]] = field(default_factory=list)

    def evaluate_one(self, *, slug: str, pr_id: int) -> MergeAttempt:
        self.calls.append((slug, pr_id))
        return MergeAttempt(
            slug=slug,
            pr_id=pr_id,
            decision="merged" if self.merged else "flag_no_review",
            merged=self.merged,
            merged_sha="b" * 40 if self.merged else "",
            reason="solo_overlay_no_clear" if self.merged else "solo_overlay_no_review",
        )


def _record(*, verdict: str = "merge_safe") -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "review",
            "record",
            str(_PR_ID),
            _SLUG,
            reviewed_sha=_HEAD,
            verdict=verdict,
            reviewer_identity="cold-reviewer-agent",
            gh_verify_result="green",
            blast_class="logic",
        ),
    )


class TestRecordTriggersSweep(TestCase):
    def test_merge_safe_verdict_triggers_an_on_demand_sweep_merge(self) -> None:
        scanner = _FakeSweepScanner(merged=True)
        with patch(f"{_SWEEP_MOD}._sweep_scanner_for_overlay", return_value=scanner):
            result = _record()

        assert result["recorded"] is True
        assert scanner.calls == [(_SLUG, _PR_ID)]

    def test_hold_verdict_does_not_trigger_a_sweep_merge(self) -> None:
        scanner = _FakeSweepScanner(merged=True)
        with patch(f"{_SWEEP_MOD}._sweep_scanner_for_overlay", return_value=scanner):
            result = _record(verdict="hold")

        assert result["recorded"] is True
        assert scanner.calls == []

    def test_sweep_trigger_failure_does_not_break_verdict_recording(self) -> None:
        with patch(f"{_SWEEP_MOD}._sweep_scanner_for_overlay", side_effect=RuntimeError("gh down")):
            result = _record()

        assert result["recorded"] is True
