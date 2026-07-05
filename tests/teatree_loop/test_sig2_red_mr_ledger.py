"""SIG-2 (#7): the RedMrFixAttempt ledger is actually populated end-to-end.

The bug: ``MyPrsScanner`` omitted ``head_sha`` from the ``my_pr.failed``
payload, so ``claim_red_mr_fix`` always saw a blank sha and fail-OPENed
(``return True`` forever) — the ledger was never written, the same red SHA
re-dispatched every tick, and S1 stayed permanently ``instrumentation_gap``.

These tests pin the fix at four levels the audit prescribed:
(a) wire-level scan->dispatch round-trip yields exactly one row / one action;
(b) forge-shape parity — GitLab top-level ``sha`` and GitHub ``head.sha``;
(c) blank-sha sentinel — one dispatch, gap surfaced (not silent, not open);
(d) live-writer feeding S1 — a scanned+claimed red PR counts NOT first-try-green.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory_signals import SignalStatus, first_try_green_rate
from teatree.core.models import RedMrFixAttempt
from teatree.loop.dispatch import _dispatch_one
from teatree.loop.dispatch_gates import claim_red_mr_fix
from teatree.loop.scanners import my_prs as my_prs_mod
from teatree.loop.scanners import pr_payload
from teatree.loop.scanners import reviewer_prs as reviewer_prs_mod
from teatree.loop.scanners.my_prs import MyPrsScanner
from tests.factories import MergeAuditFactory, MergeClearFactory
from tests.teatree_loop.test_scanners import FakeCodeHost


def _gitlab_failed_pr(*, iid: int = 7, sha: str = "abc123", url: str = "") -> dict[str, object]:
    return {
        "iid": iid,
        "title": "Fix thing",
        "web_url": url or f"https://gitlab.com/souliane/teatree/-/merge_requests/{iid}",
        "sha": sha,
        "head_pipeline": {"status": "failed"},
    }


def _github_failed_pr(*, number: int = 9, sha: str = "deadbeef", url: str = "") -> dict[str, object]:
    return {
        "number": number,
        "title": "Fix thing",
        "html_url": url or f"https://github.com/souliane/teatree/pull/{number}",
        "head": {"sha": sha},
        "status_check_rollup": {"state": "failure"},
    }


class TestHeadShaSsot:
    """One helper, two importers — the sibling-reimplementation family killer."""

    def test_my_prs_and_reviewer_prs_reference_the_same_symbol(self) -> None:
        assert my_prs_mod.head_sha is pr_payload.head_sha
        assert reviewer_prs_mod.head_sha is pr_payload.head_sha


class TestMyPrsEmitsHeadSha:
    """Every emitted ``my_pr.*`` signal carries the head sha via the shared helper."""

    def test_failed_signal_carries_head_sha_gitlab_shape(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_gitlab_failed_pr(sha="abc123")])
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]
        assert signals[0].payload["head_sha"] == "abc123"

    def test_failed_signal_carries_head_sha_github_shape(self) -> None:
        # Forge-shape parity: GitHub nests the sha under ``head.sha``.
        host = FakeCodeHost(user="alice", my_prs=[_github_failed_pr(sha="deadbeef")])
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]
        assert signals[0].payload["head_sha"] == "deadbeef"

    def test_open_signal_also_carries_head_sha(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "iid": 3,
                    "title": "Clean PR",
                    "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/3",
                    "sha": "cafef00d",
                    "head_pipeline": {"status": "success"},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.open"]
        assert signals[0].payload["head_sha"] == "cafef00d"

    def test_draft_notes_signal_also_carries_head_sha(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "iid": 4,
                    "title": "Notes PR",
                    "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/4",
                    "sha": "b0bab0ba",
                    "head_pipeline": {"status": "running"},
                    "user_notes_count": 2,
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.draft_notes"]
        assert signals[0].payload["head_sha"] == "b0bab0ba"

    def test_missing_sha_emits_blank_head_sha_not_absent_key(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "iid": 5,
                    "title": "No sha PR",
                    "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/5",
                    "head_pipeline": {"status": "failed"},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert signals[0].payload["head_sha"] == ""


class TestClaimRedMrFixWireRoundTrip(TestCase):
    """(a) The same failing PR through scan->dispatch->claim TWICE => one row, one action."""

    def _agent_action_payload(self, pr: dict[str, object]) -> dict[str, object]:
        host = FakeCodeHost(user="alice", my_prs=[pr])
        signals = MyPrsScanner(host=host).scan()
        actions = _dispatch_one(signals[0])
        agent = [a for a in actions if a.kind == "agent"]
        assert len(agent) == 1, actions
        assert agent[0].zone == "t3:debug"
        return agent[0].payload

    def test_two_ticks_same_red_sha_yield_one_claim_and_one_row(self) -> None:
        payload = self._agent_action_payload(_gitlab_failed_pr(sha="abc123"))
        first = claim_red_mr_fix(payload)
        second = claim_red_mr_fix(payload)
        assert first is True
        assert second is False
        assert RedMrFixAttempt.objects.count() == 1
        row = RedMrFixAttempt.objects.get()
        assert row.head_sha == "abc123"

    def test_new_sha_after_first_dispatch_claims_again(self) -> None:
        first = claim_red_mr_fix(self._agent_action_payload(_gitlab_failed_pr(iid=7, sha="aaa")))
        moved = claim_red_mr_fix(self._agent_action_payload(_gitlab_failed_pr(iid=7, sha="bbb")))
        assert first is True
        assert moved is True
        assert RedMrFixAttempt.objects.count() == 2


class TestClaimRedMrFixHardening(TestCase):
    """(c) blank-sha sentinel + raw fallback + DB fail-open-with-warning."""

    URL = "https://github.com/souliane/teatree/pull/12"

    def test_extracts_sha_from_raw_when_head_sha_blank(self) -> None:
        # A payload with no top-level head_sha but a ``raw`` dict carrying it
        # (GitHub shape) still claims a real sha via the shared helper.
        payload = {"url": self.URL, "raw": {"head": {"sha": "fromraw"}}}
        assert claim_red_mr_fix(payload) is True
        assert RedMrFixAttempt.objects.get().head_sha == "fromraw"

    def test_blank_sha_sentinel_dispatches_exactly_once(self) -> None:
        # No sha anywhere: the old code fail-OPENed (True forever, zero rows).
        # Now: one sentinel claim keyed on pr_url, then no further dispatch.
        payload = {"url": self.URL}
        first = claim_red_mr_fix(payload)
        second = claim_red_mr_fix(payload)
        assert first is True
        assert second is False
        assert RedMrFixAttempt.objects.filter(pr_url=self.URL).count() == 1

    def test_blank_sha_surfaces_the_gap_not_silent(self) -> None:
        with self.assertLogs("teatree.loop.dispatch_gates", level="WARNING") as logs:
            claim_red_mr_fix({"url": self.URL})
        assert any(self.URL in message for message in logs.output)

    def test_database_error_fails_open_and_warns(self) -> None:
        payload = {"url": self.URL, "head_sha": "abc123"}
        with (
            patch.object(RedMrFixAttempt, "claim", side_effect=DatabaseError("boom")),
            self.assertLogs("teatree.loop.dispatch_gates", level="WARNING") as logs,
        ):
            result = claim_red_mr_fix(payload)
        assert result is True
        assert any(self.URL in message for message in logs.output)

    def test_no_pr_url_returns_true_without_a_row(self) -> None:
        assert claim_red_mr_fix({"head_sha": "abc123"}) is True
        assert RedMrFixAttempt.objects.count() == 0


class TestS1LiveWriterEndToEnd(TestCase):
    """(d) A scanned + claimed red PR feeds S1: counted NOT first-try-green, status OK.

    RED before the fix: my_prs emitted no head_sha, so ``claim_red_mr_fix`` wrote
    ZERO RedMrFixAttempt rows -> every merge read first-try-green with a silent
    recorder -> S1 ``instrumentation_gap``. GREEN after: the live claim writes the
    row, the red PR is excluded from first-try-green, and S1 reports a real rate.
    """

    SLUG = "souliane/teatree"

    def _seed_merges(self, now: datetime, pr_ids: range) -> None:
        for pr_id in pr_ids:
            merged_at = now - timedelta(days=5)
            clear = MergeClearFactory(
                pr_id=pr_id,
                slug=self.SLUG,
                issued_at=merged_at - timedelta(hours=1),
                consumed_at=merged_at,
            )
            MergeAuditFactory(clear=clear, merged_at=merged_at)

    def test_scanned_red_pr_counts_not_first_try_green(self) -> None:
        now = timezone.now()
        self._seed_merges(now, range(901, 906))  # 5 merges -> denom == 5

        # Live producer: scan the red PR, dispatch, claim — writes the ledger row.
        host = FakeCodeHost(
            user="alice",
            my_prs=[_github_failed_pr(number=901, sha="cafe1234", url=f"https://github.com/{self.SLUG}/pull/901")],
        )
        signals = MyPrsScanner(host=host).scan()
        agent = [a for a in _dispatch_one(signals[0]) if a.kind == "agent"]
        assert claim_red_mr_fix(agent[0].payload) is True

        reading = first_try_green_rate(now=now)
        assert reading.status == SignalStatus.OK
        assert reading.status != SignalStatus.INSTRUMENTATION_GAP
        assert reading.sample_size == 5
        assert reading.value == pytest.approx(0.8)

    def test_dead_recorder_stays_instrumentation_gap_control(self) -> None:
        # Control twin: no red rows at all (a genuinely silent recorder) still
        # trips instrumentation_gap — proving the live-writer test above is not
        # vacuously OK for some unrelated reason.
        now = timezone.now()
        self._seed_merges(now, range(901, 906))
        reading = first_try_green_rate(now=now)
        assert reading.status == SignalStatus.INSTRUMENTATION_GAP
