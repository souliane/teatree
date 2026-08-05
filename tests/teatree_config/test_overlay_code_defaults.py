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
    cold_overlay_code_defaults,
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


class TestColdRead:
    """The tier a PreToolUse hook sees, where no provider is ever registered.

    ``build_and_register`` runs at ``teatree.core.overlay_loader`` import time, which
    a cold hook never reaches — so without this read the tier is structurally absent
    there and a declaration in ``overlay_settings.py`` cannot reach the gate that
    reads it. ``t3-teatree`` is the installed overlay every core run has.
    """

    def test_declared_constant_is_read_without_the_provider(self) -> None:
        assert cold_overlay_code_defaults("t3-teatree")["review_skill"] == "ac-reviewing-codebase"

    def test_an_undeclared_key_is_absent_rather_than_defaulted(self) -> None:
        assert "single_branch_repos" not in cold_overlay_code_defaults("t3-teatree")

    def test_only_promoted_keys_are_read(self) -> None:
        assert set(cold_overlay_code_defaults("t3-teatree")) <= PROMOTED_OVERLAY_CODE_DEFAULT_KEYS

    def test_an_unknown_overlay_fails_safe_to_empty(self) -> None:
        assert cold_overlay_code_defaults("no-such-overlay") == {}

    def test_the_registered_provider_still_wins(self) -> None:
        original = seam._provider
        try:
            register_overlay_code_default_provider(lambda name: {"review_skill": "from-provider"})
            assert overlay_code_defaults("t3-teatree") == {"review_skill": "from-provider"}
        finally:
            register_overlay_code_default_provider(original)

    def test_the_cold_read_backs_an_unregistered_provider(self) -> None:
        original = seam._provider
        try:
            register_overlay_code_default_provider(_unregistered_provider)
            assert overlay_code_defaults("t3-teatree")["review_skill"] == "ac-reviewing-codebase"
        finally:
            register_overlay_code_default_provider(original)
