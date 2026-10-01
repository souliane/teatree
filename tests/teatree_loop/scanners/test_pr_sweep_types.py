"""Unit tests for the pure attempt builders in ``pr_sweep_types`` (#4856)."""

from teatree.loop.scanners.pr_sweep_types import PrSummary, blocked_merge_attempt

_PR = PrSummary(slug="souliane/teatree", number=6230, head_sha="a" * 40, is_draft=False, has_changes_requested=False)


class TestBlockedMergeAttempt:
    def test_a_refusal_is_prefixed_with_its_first_line(self) -> None:
        attempt = blocked_merge_attempt(
            _PR, reason_prefix="solo_overlay_merge_refused", refusal="no rubric is recorded", default_reason="fallback"
        )
        assert attempt.slug == _PR.slug
        assert attempt.pr_id == _PR.number
        assert attempt.decision == "blocked"
        assert attempt.merged is False
        assert attempt.reason == "solo_overlay_merge_refused: no rubric is recorded"

    def test_only_the_first_line_of_a_multiline_refusal_is_kept(self) -> None:
        attempt = blocked_merge_attempt(
            _PR,
            reason_prefix="fallback_uv_audit_gh_refused",
            refusal="head moved\nretried twice",
            default_reason="fallback",
        )
        assert attempt.reason == "fallback_uv_audit_gh_refused: head moved"

    def test_an_empty_refusal_falls_back_to_the_default_reason(self) -> None:
        attempt = blocked_merge_attempt(
            _PR, reason_prefix="fallback_uv_audit_gh_refused", refusal="", default_reason="fb"
        )
        assert attempt.reason == "fb"

    def test_an_empty_refusal_with_no_default_falls_back_to_the_bare_prefix(self) -> None:
        # The common case (#4856): the solo-overlay call site has no distinct fallback
        # of its own, so omitting default_reason reuses reason_prefix verbatim.
        attempt = blocked_merge_attempt(_PR, reason_prefix="solo_overlay_merge_refused", refusal="")
        assert attempt.reason == "solo_overlay_merge_refused"
