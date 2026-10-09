"""The classifier -> durable-cooldown seam, including the false-positive control."""

from types import SimpleNamespace

from django.test import TestCase

from teatree.agents.codex_app_server_errors import provider_fallback
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.agents.runner import _retry_route_exception
from teatree.agents.runner_route_recording import NO_ROUTE_FALLBACK
from teatree.core.models import ReviewBackendCooldown
from teatree.core.review.backend_cooldown import record_quota_exhaustion

_REVIEW_BODY_DISCUSSING_LIMITS = (
    "## Findings\n"
    "1. `fetch_pages` has no rate limit backoff — a 429 from the forge retries "
    "immediately and burns the remaining quota.\n"
    "Verdict: hold"
)


class TestRecordQuotaExhaustion(TestCase):
    def test_codex_review_quota_failure_writes_the_cooldown_used_by_backend_selection(self) -> None:
        task = SimpleNamespace(ticket=SimpleNamespace(overlay="acme"))
        preflight = SimpleNamespace(
            dispatch=SimpleNamespace(name="codex_app_server", route_candidate_index=None, rejected=()), skills=()
        )
        retry = SimpleNamespace(phase="codex_reviewing")
        error = HarnessFallbackError(
            "Codex provider reported a retryable quota failure during turn/completed.",
            kind=HarnessFallbackKind.QUOTA_EXHAUSTED,
        )

        assert (
            _retry_route_exception(
                error,
                task=task,
                preflight=preflight,
                route_retry=retry,
                route_fallback=NO_ROUTE_FALLBACK,
            )
            is None
        )
        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="acme") is True

    def test_non_review_quota_failure_does_not_cool_the_review_backend(self) -> None:
        task = SimpleNamespace(ticket=SimpleNamespace(overlay="acme"))
        preflight = SimpleNamespace(
            dispatch=SimpleNamespace(name="codex_app_server", route_candidate_index=None, rejected=()), skills=()
        )
        retry = SimpleNamespace(phase="coding")
        error = HarnessFallbackError("quota failure", kind=HarnessFallbackKind.QUOTA)

        _retry_route_exception(
            error,
            task=task,
            preflight=preflight,
            route_retry=retry,
            route_fallback=NO_ROUTE_FALLBACK,
        )
        assert ReviewBackendCooldown.objects.exists() is False

    def test_session_budget_and_rate_limit_do_not_cool_review_backend(self) -> None:
        task = SimpleNamespace(ticket=SimpleNamespace(overlay="acme"))
        preflight = SimpleNamespace(
            dispatch=SimpleNamespace(name="codex_app_server", route_candidate_index=None, rejected=()), skills=()
        )
        retry = SimpleNamespace(phase="codex_reviewing")
        for message in ("session budget quota exhausted", "rate limit quota exceeded"):
            error = HarnessFallbackError(message, kind=HarnessFallbackKind.QUOTA)
            _retry_route_exception(
                error,
                task=task,
                preflight=preflight,
                route_retry=retry,
                route_fallback=NO_ROUTE_FALLBACK,
            )
        assert ReviewBackendCooldown.objects.exists() is False

    def test_provider_classifies_only_usage_limit_as_account_exhaustion(self) -> None:
        for code in ("sessionBudgetExceeded", "rateLimitExceeded"):
            failure = provider_fallback(code, context="turn/completed")
            assert isinstance(failure, HarnessFallbackError)
            assert failure.kind is HarnessFallbackKind.QUOTA
        exhausted = provider_fallback("usageLimitExceeded", context="turn/completed")
        assert isinstance(exhausted, HarnessFallbackError)
        assert exhausted.kind is HarnessFallbackKind.QUOTA_EXHAUSTED

    def test_other_harness_quota_failure_in_review_does_not_cool_codex(self) -> None:
        task = SimpleNamespace(ticket=SimpleNamespace(overlay="acme"))
        preflight = SimpleNamespace(
            dispatch=SimpleNamespace(name="claude_sdk", route_candidate_index=None, rejected=()), skills=()
        )
        retry = SimpleNamespace(phase="codex_reviewing")
        error = HarnessFallbackError("quota failure", kind=HarnessFallbackKind.QUOTA)

        _retry_route_exception(
            error,
            task=task,
            preflight=preflight,
            route_retry=retry,
            route_fallback=NO_ROUTE_FALLBACK,
        )
        assert ReviewBackendCooldown.objects.exists() is False

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
