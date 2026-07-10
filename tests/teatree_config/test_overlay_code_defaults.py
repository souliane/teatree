# test-path: mirror
"""The overlay-code-default registration seam (#36).

Pure-logic unit coverage of the inverted-dependency seam: the unregistered
default fails safe to ``{}``, an empty overlay name short-circuits, and a
registered provider is consulted. The promoted-key set is pinned against the
live ``OverlayConfig`` / ``UserSettings`` fields so a key can never be promoted
without a backing field on both sides.
"""

import dataclasses

import teatree.config.overlay_code_defaults as seam
from teatree.config.overlay_code_defaults import (
    PROMOTED_OVERLAY_CODE_DEFAULT_KEYS,
    _unregistered_provider,
    overlay_code_defaults,
    register_overlay_code_default_provider,
)
from teatree.config.settings import UserSettings
from teatree.core.overlay import OverlayConfig


def test_unregistered_provider_returns_empty() -> None:
    assert _unregistered_provider("t3-teatree") == {}


def test_empty_overlay_name_short_circuits() -> None:
    assert overlay_code_defaults("") == {}


def test_register_swaps_the_active_provider() -> None:
    original = seam._provider
    try:
        register_overlay_code_default_provider(lambda name: {"review_skill": f"from-{name}"})
        assert overlay_code_defaults("my-overlay") == {"review_skill": "from-my-overlay"}
    finally:
        register_overlay_code_default_provider(original)


def test_promoted_keys_are_all_overlay_config_and_user_settings_fields() -> None:
    user_fields = {f.name for f in dataclasses.fields(UserSettings)}
    overlay_fields = set(OverlayConfig.model_fields)
    assert user_fields >= PROMOTED_OVERLAY_CODE_DEFAULT_KEYS
    assert overlay_fields >= PROMOTED_OVERLAY_CODE_DEFAULT_KEYS
