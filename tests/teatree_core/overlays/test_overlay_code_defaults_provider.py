# test-path: mirror
"""Core-side provider for the overlay-code-default seam (#36).

``build_and_register`` wires a reader that pulls the promoted keys off an
overlay's ``OverlayConfig`` via an injected ``get_overlay``, and fails safe to
``{}`` when the overlay cannot be resolved. Covered here without the real
registry by injecting a stub getter and reading back through the config seam.
"""

from types import SimpleNamespace

from django.core.exceptions import ImproperlyConfigured

import teatree.config.overlay_code_defaults as seam
from teatree.config.overlay_code_defaults import (
    PROMOTED_OVERLAY_CODE_DEFAULT_KEYS,
    overlay_code_defaults,
    register_overlay_code_default_provider,
)
from teatree.core.overlay import OverlayConfig
from teatree.core.overlays.overlay_code_defaults_provider import build_and_register


def test_registered_provider_reads_promoted_keys_off_the_config() -> None:
    original = seam._provider
    config = OverlayConfig()
    config.review_skill = "stub-skill"
    try:
        build_and_register(lambda name: SimpleNamespace(config=config))
        resolved = overlay_code_defaults("any-overlay")
    finally:
        register_overlay_code_default_provider(original)
    assert set(resolved) == set(PROMOTED_OVERLAY_CODE_DEFAULT_KEYS)
    assert resolved["review_skill"] == "stub-skill"
    assert resolved["scanning_news_skill"] == "scanning-news"


def test_provider_fails_safe_when_overlay_unresolvable() -> None:
    original = seam._provider

    def _raises(name: str) -> SimpleNamespace:
        raise ImproperlyConfigured(name)

    try:
        build_and_register(_raises)
        assert overlay_code_defaults("missing-overlay") == {}
    finally:
        register_overlay_code_default_provider(original)
