"""The durable cooldown that stops `auto` re-probing an exhausted review backend."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ReviewBackendCooldown


class TestCooldownLifecycle(TestCase):
    def test_a_started_cooldown_is_live_until_its_ttl_elapses(self) -> None:
        ReviewBackendCooldown.start(backend="codex", overlay="acme", signature="usage limit", ttl_hours=6)

        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="acme") is True

    def test_an_expired_cooldown_stops_cooling_with_no_actor(self) -> None:
        row = ReviewBackendCooldown.start(backend="codex", overlay="acme", ttl_hours=6)
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["expires_at"])

        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="acme") is False

    def test_no_cooldown_means_not_cooling(self) -> None:
        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="acme") is False

    def test_a_second_exhaustion_extends_the_same_row_rather_than_stacking(self) -> None:
        first = ReviewBackendCooldown.start(backend="codex", overlay="acme", ttl_hours=1)
        second = ReviewBackendCooldown.start(backend="codex", overlay="acme", ttl_hours=6)

        assert second.pk == first.pk
        assert second.expires_at > first.expires_at
        assert ReviewBackendCooldown.objects.filter(backend="codex").count() == 1

    def test_the_tripping_signature_is_kept_for_the_audit_trail(self) -> None:
        row = ReviewBackendCooldown.start(backend="codex", signature="insufficient credit", ttl_hours=6)

        assert row.signature == "insufficient credit"


class TestCooldownScoping(TestCase):
    def test_a_cooldown_on_one_overlay_leaves_another_free(self) -> None:
        ReviewBackendCooldown.start(backend="codex", overlay="acme", ttl_hours=6)

        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="other") is False

    def test_an_unscoped_cooldown_parks_every_overlay(self) -> None:
        # An exhaustion recorded before any overlay attribution is a fact about the
        # ACCOUNT, so it must park the backend everywhere rather than nowhere.
        ReviewBackendCooldown.start(backend="codex", ttl_hours=6)

        assert ReviewBackendCooldown.is_cooling(backend="codex", overlay="acme") is True
        assert ReviewBackendCooldown.is_cooling(backend="codex") is True

    def test_a_cooldown_on_one_backend_leaves_the_other_free(self) -> None:
        ReviewBackendCooldown.start(backend="codex", ttl_hours=6)

        assert ReviewBackendCooldown.is_cooling(backend="claude") is False
