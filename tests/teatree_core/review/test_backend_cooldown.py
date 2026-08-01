"""The classifier -> durable-cooldown seam, including the false-positive control."""

from django.test import TestCase

from teatree.core.models import ReviewBackendCooldown
from teatree.core.review.backend_cooldown import record_quota_exhaustion

_REVIEW_BODY_DISCUSSING_LIMITS = (
    "## Findings\n"
    "1. `fetch_pages` has no rate limit backoff — a 429 from the forge retries "
    "immediately and burns the remaining quota.\n"
    "Verdict: hold"
)


class TestRecordQuotaExhaustion(TestCase):
    def test_a_failed_run_out_of_quota_parks_the_backend(self) -> None:
        signature = record_quota_exhaustion(
            backend="codex",
            overlay="acme",
            returncode=1,
            stderr="You have hit your usage limit for this plan.",
        )

        assert signature == "usage limit"
        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="acme") is True

    def test_an_ordinary_failure_records_nothing(self) -> None:
        signature = record_quota_exhaustion(
            backend="codex",
            overlay="acme",
            returncode=1,
            stderr="error: unknown flag --nope",
        )

        assert signature == ""
        assert ReviewBackendCooldown.objects.exists() is False

    def test_a_successful_review_whose_findings_discuss_rate_limits_never_parks(self) -> None:
        # The false-positive control at the seam that actually writes: a review that
        # WORKED must not cool the backend for hours because its findings mention a 429.
        signature = record_quota_exhaustion(
            backend="codex",
            overlay="acme",
            returncode=0,
            stderr=_REVIEW_BODY_DISCUSSING_LIMITS,
        )

        assert signature == ""
        assert ReviewBackendCooldown.objects.exists() is False
        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="acme") is False

    def test_the_recorded_ttl_comes_from_settings(self) -> None:
        record_quota_exhaustion(backend="codex", returncode=1, stderr="quota exceeded")

        row = ReviewBackendCooldown.objects.get(backend="codex")
        assert (row.expires_at - row.started_at).total_seconds() == 6 * 3600
