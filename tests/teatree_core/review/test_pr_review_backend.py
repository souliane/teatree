"""`pr_review_backend` resolution — auto degrades, an explicit pin never does."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase

from teatree.config import PrReviewBackend, UserSettings
from teatree.core.models import ReviewBackendCooldown
from teatree.core.review.pr_review_backend import codex_is_available, resolve_pr_review_backend


@contextmanager
def _configured(backend: PrReviewBackend, *, codex_installed: bool = True) -> Iterator[None]:
    with (
        patch(
            "teatree.core.review.pr_review_backend.get_effective_settings",
            return_value=UserSettings(pr_review_backend=backend),
        ),
        patch(
            "teatree.core.review.pr_review_backend.shutil.which",
            return_value="/usr/local/bin/codex" if codex_installed else None,
        ),
    ):
        yield


class TestAutoResolution(TestCase):
    def test_auto_prefers_codex_when_it_can_serve(self) -> None:
        with _configured(PrReviewBackend.AUTO):
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CODEX

    def test_auto_falls_back_to_claude_with_no_codex_binary(self) -> None:
        with _configured(PrReviewBackend.AUTO, codex_installed=False):
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CLAUDE

    def test_auto_falls_back_to_claude_while_codex_is_cooling_down(self) -> None:
        ReviewBackendCooldown.start(backend="codex", overlay="acme", signature="usage limit", ttl_hours=6)
        with _configured(PrReviewBackend.AUTO):
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CLAUDE

    def test_auto_returns_to_codex_once_the_cooldown_covers_another_overlay_only(self) -> None:
        ReviewBackendCooldown.start(backend="codex", overlay="other", ttl_hours=6)
        with _configured(PrReviewBackend.AUTO):
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CODEX

    def test_auto_never_returns_itself(self) -> None:
        with _configured(PrReviewBackend.AUTO, codex_installed=False):
            assert resolve_pr_review_backend("acme") is not PrReviewBackend.AUTO


class TestExplicitPinsNeverDegrade(TestCase):
    def test_a_codex_pin_survives_a_missing_binary(self) -> None:
        with _configured(PrReviewBackend.CODEX, codex_installed=False):
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CODEX

    def test_a_codex_pin_survives_a_live_cooldown(self) -> None:
        ReviewBackendCooldown.start(backend="codex", overlay="acme", ttl_hours=6)
        with _configured(PrReviewBackend.CODEX):
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CODEX

    def test_a_claude_pin_is_never_upgraded_to_codex(self) -> None:
        with _configured(PrReviewBackend.CLAUDE):
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CLAUDE


class TestCodexAvailability(TestCase):
    def test_unavailable_without_the_binary(self) -> None:
        with _configured(PrReviewBackend.AUTO, codex_installed=False):
            assert codex_is_available(overlay="acme") is False

    def test_unavailable_while_cooling(self) -> None:
        ReviewBackendCooldown.start(backend="codex", overlay="acme", ttl_hours=6)
        with _configured(PrReviewBackend.AUTO):
            assert codex_is_available(overlay="acme") is False

    def test_available_when_installed_and_not_cooling(self) -> None:
        with _configured(PrReviewBackend.AUTO):
            assert codex_is_available(overlay="acme") is True

    def test_an_unreadable_cooldown_degrades_to_unavailable(self) -> None:
        # The builder runs in contexts with no usable DB. "I cannot tell" must not
        # read as "not cooling" — that is the exhausted account the cooldown exists
        # to stop re-probing. Claude is always available, so the safe branch is free.
        with (
            _configured(PrReviewBackend.AUTO),
            patch(
                "teatree.core.review.pr_review_backend.ReviewBackendCooldown.is_cooling",
                side_effect=RuntimeError("Database access not allowed"),
            ),
        ):
            assert codex_is_available(overlay="acme") is False
            assert resolve_pr_review_backend("acme") is PrReviewBackend.CLAUDE
