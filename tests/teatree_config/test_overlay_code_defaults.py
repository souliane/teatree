# test-path: mirror
"""The overlay-code-default registration seam (#36).

Pure-logic unit coverage of the inverted-dependency seam: the unregistered
default fails safe to ``{}``, an empty overlay name short-circuits, and a
registered provider is consulted. The promoted-key set is pinned against the
live ``OverlayConfig`` / ``UserSettings`` fields so a key can never be promoted
without a backing field on both sides.
"""

import dataclasses
import logging
import tomllib

import pytest

import teatree.config.overlay_code_defaults as seam
from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.overlay_code_defaults import (
    PROMOTED_OVERLAY_CODE_DEFAULT_KEYS,
    _unregistered_provider,
    cold_overlay_code_defaults,
    overlay_code_defaults,
    register_overlay_code_default_provider,
)
from teatree.config.schema import _DEFAULTS_TOML
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


def test_every_promoted_code_default_equals_the_shipped_file_value() -> None:
    # The provider hands the resolver EVERY promoted key off the OverlayConfig class
    # default, declared or not, and that tier sits ABOVE `defaults.toml` — so with any
    # overlay active the file's value for these nine keys is never the one in force.
    # `test_toml_default_tier.py` exempts them from its no-unapproved-divergence guard, and
    # unlike the AUTONOMY exemption beside it nothing bounded that hole: a maintainer
    # following the file's own "HAND-EDITABLE. Edit a value here and the box serves it"
    # header ships a value the box does not read, with no test going red. This is the
    # bound — the two sources agree by construction or this fails.
    shipped = flatten_settings_table(tomllib.loads(_DEFAULTS_TOML.read_text(encoding="utf-8"))["teatree"])
    config = OverlayConfig()
    drift = {
        key: (getattr(config, key), shipped[key])
        for key in sorted(PROMOTED_OVERLAY_CODE_DEFAULT_KEYS)
        if getattr(config, key) != shipped[key]
    }
    assert not drift, f"the overlay code default outranks a shipped value it disagrees with: {drift}"


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


class TestABrokenSettingsModuleIsAudible:
    """A settings module that stopped importing takes its declarations down QUIETLY.

    Both tiers fail safe to ``{}`` and the resolver then serves the shipped default, so an
    overlay declaring ``single_branch_repos`` gets an inert gate — the exact harm the
    module docstring names as the reason the cold read exists. The empty return is right;
    the silence is what leaves nobody able to tell an undeclared key from a broken one.
    """

    def test_an_import_failure_is_logged_with_the_overlay_and_the_module(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _explode(module_path: str) -> object:
            raise ModuleNotFoundError(module_path)

        monkeypatch.setattr(seam, "import_module", _explode)
        with caplog.at_level(logging.WARNING, logger=seam.__name__):
            assert cold_overlay_code_defaults("t3-teatree") == {}
        assert "t3-teatree" in caplog.text
        assert seam.OVERLAY_SETTINGS_MODULE_LEAF in caplog.text

    def test_an_overlay_with_no_entry_point_stays_quiet(self, caplog: pytest.LogCaptureFixture) -> None:
        # Control: an overlay this box does not have declared nothing to lose. Warning on
        # it would make the signal noise, and the noise is what gets muted.
        with caplog.at_level(logging.WARNING, logger=seam.__name__):
            assert cold_overlay_code_defaults("no-such-overlay") == {}
        assert caplog.text == ""
