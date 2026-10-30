"""The CI-block remedy gate and the main uv-audit probe the PR sweep shares (#4063)."""

import logging
from unittest.mock import create_autospec

import pytest

from teatree.loop.scanners.pr_sweep_ci_classify import ci_block_attempt, main_uv_audit_red
from teatree.loop.scanners.pr_sweep_ports import PrApiClient
from teatree.loop.scanners.pr_sweep_types import UV_AUDIT_CHECK_NAME, MergeAttempt, PrSummary

SLUG = "souliane/teatree"
PR_ID = 5689


def _pr(*, behind_main: bool = True, is_conflicted: bool = False) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=PR_ID,
        head_sha="a" * 40,
        is_draft=False,
        has_changes_requested=False,
        url=f"https://github.com/{SLUG}/pull/{PR_ID}",
        behind_main=behind_main,
        is_conflicted=is_conflicted,
    )


class TestCiBlockAttempt:
    @pytest.mark.parametrize(
        ("reason", "failing", "behind_main", "conflicted", "remedied"),
        [
            ("ci_red", {"test"}, True, False, True),
            ("uv_audit_red_but_clean_on_main", {UV_AUDIT_CHECK_NAME}, True, False, True),
            ("ci_red", {"test"}, False, False, False),
            ("ci_red", {"test"}, True, True, False),
            ("ci_pending", {"test"}, True, False, False),
            ("required_checks_indeterminate", set(), True, False, False),
        ],
        ids=[
            "red-on-a-stale-base",
            "uv-audit-red-on-a-stale-base",
            "red-on-an-up-to-date-branch",
            "conflicted-needs-a-human",
            "still-running",
            "indeterminate-fails-closed",
        ],
    )
    def test_only_a_red_verdict_on_a_stale_base_is_remedied(
        self, reason: str, failing: set[str], *, behind_main: bool, conflicted: bool, remedied: bool
    ) -> None:
        remedy_calls: list[PrSummary] = []

        def remedy(pr: PrSummary) -> MergeAttempt:
            remedy_calls.append(pr)
            return MergeAttempt(
                slug=pr.slug, pr_id=pr.number, decision="needs_branch_update", reason="update_requested"
            )

        pr = _pr(behind_main=behind_main, is_conflicted=conflicted)

        attempt = ci_block_attempt(pr, reason=reason, failing=failing, remedy=remedy)

        assert remedy_calls == ([pr] if remedied else [])
        assert attempt.decision == ("needs_branch_update" if remedied else "skip")
        assert attempt.reason == ("update_requested" if remedied else reason)
        assert attempt.failing_required == tuple(sorted(failing))
        assert attempt.base_current is (not behind_main)
        assert attempt.url == pr.url


class TestMainUvAuditRed:
    @pytest.mark.parametrize("red", [True, False])
    def test_reports_the_forge_answer_for_the_uv_audit_check(self, *, red: bool) -> None:
        api = create_autospec(PrApiClient, instance=True)
        api.main_check_failed.return_value = red

        assert main_uv_audit_red(api, slug=SLUG) is red

        api.main_check_failed.assert_called_once_with(slug=SLUG, check_name=UV_AUDIT_CHECK_NAME)

    def test_unreadable_main_reads_as_not_red_and_logs_why(self, caplog: pytest.LogCaptureFixture) -> None:
        api = create_autospec(PrApiClient, instance=True)
        api.main_check_failed.side_effect = RuntimeError("forge unreachable")

        with caplog.at_level(logging.ERROR, logger="teatree.loop.scanners.pr_sweep_ci_classify"):
            assert main_uv_audit_red(api, slug=SLUG) is False

        assert f"failed to fetch main uv-audit status for {SLUG}" in caplog.text
