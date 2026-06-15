# test-path: cross-cutting
"""DB override tier in the effective-settings resolution chain (#1775).

The DB tier sits between the env layer (which still wins) and the per-overlay
TOML layer in the documented precedence:

    env -> DB -> per-overlay TOML -> global [teatree] -> dataclass default

Pilot setting: ``issue_implementer_enabled`` (a boolean kill-switch, default
``False``, already env- and overlay-overridable) so an EMPTY table is a provable
no-op and the three-way precedence is observable.

Integration-first: real TOML fixtures under ``tmp_path`` with
``teatree.config.CONFIG_PATH`` monkeypatched, against the real DB.
"""

from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting

from ._shared import _write_toml


class TestDbConfigTier(TestCase):
    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        # No active overlay env -> the active-overlay path resolves to no
        # per-overlay overrides; env override is asserted explicitly per test.
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        self.monkeypatch = monkeypatch

    def test_empty_table_is_a_no_op(self) -> None:
        # Global TOML leaves the pilot at its dataclass default (False). An
        # empty ConfigSetting table must not change that — anti-vacuous: the
        # resolved value is identical to today's file/env resolution.
        _write_toml(self.config_path, "[teatree]\n")
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().issue_implementer_enabled is False

    def test_db_row_wins_over_global_toml(self) -> None:
        _write_toml(self.config_path, "[teatree]\nissue_implementer_enabled = false\n")
        # Sanity: without a DB row the global TOML (false) is the resolved value.
        assert get_effective_settings().issue_implementer_enabled is False
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert get_effective_settings().issue_implementer_enabled is True

    def test_db_row_wins_over_per_overlay_toml(self) -> None:
        _write_toml(
            self.config_path,
            """
[teatree]
mode = "interactive"

[overlays.my-overlay]
class = "x.y:Z"
issue_implementer_enabled = false
""",
        )
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        # Per-overlay TOML resolves false without a DB row.
        assert get_effective_settings().issue_implementer_enabled is False
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert get_effective_settings().issue_implementer_enabled is True

    def test_env_wins_over_db_row(self) -> None:
        _write_toml(self.config_path, "[teatree]\nissue_implementer_enabled = false\n")
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        # DB says false; env says true -> env wins (highest tier).
        self.monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "true")
        assert get_effective_settings().issue_implementer_enabled is True

    def test_db_row_for_non_overridable_key_is_ignored(self) -> None:
        # The pilot is scoped to OVERLAY_OVERRIDABLE_SETTINGS so an unknown /
        # non-overridable key never silently mutates the resolved settings.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("not_a_real_setting", "boom")
        # Resolution does not raise and the pilot is unchanged.
        assert get_effective_settings().issue_implementer_enabled is False

    def test_db_row_value_is_coerced_via_registry_parser(self) -> None:
        # A stored truthy non-bool is coerced by the overridable-settings parser
        # (bool) so the resolved field type stays correct.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", "5")
        assert get_effective_settings().issue_implementer_max_concurrent == 5

    def test_clear_restores_fall_through(self) -> None:
        _write_toml(self.config_path, "[teatree]\nissue_implementer_enabled = false\n")
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert get_effective_settings().issue_implementer_enabled is True
        ConfigSetting.objects.clear("issue_implementer_enabled")
        assert get_effective_settings().issue_implementer_enabled is False

    def test_bool_row_false_resolves_false(self) -> None:
        # #258 blocker 2: a stored real-bool ``False`` for an opt-in safety
        # setting must resolve to Python ``False`` — never truthy-coerced on.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("allow_destructive_disk", value=False)
        assert get_effective_settings().allow_destructive_disk is False

    def test_quoted_bool_string_row_does_not_silently_enable(self) -> None:
        # #258 blocker 2 at the READ tier: a row storing the JSON STRING
        # ``"false"`` (bypassing the write-time gate, e.g. an old row) must NOT
        # silently enable the opt-in setting via ``bool("false") == True``. The
        # strict parser rejects the ambiguous value rather than coercing it on.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("allow_destructive_disk", "false")
        with pytest.raises(ValueError, match="allow_destructive_disk"):
            get_effective_settings()

    def test_bool_row_for_int_setting_is_rejected_loud(self) -> None:
        # #258 fix round 2, blocker 1.1 at the READ tier: a row storing JSON
        # ``true`` for an int-typed setting (an out-of-band corruption, since the
        # write gate now rejects it) must be raised LOUD with the offending key,
        # never silently coerced via ``int(True) == 1``.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", value=True)
        with pytest.raises(ValueError, match="issue_implementer_max_concurrent"):
            get_effective_settings()

    def test_scalar_row_for_list_setting_is_rejected_loud(self) -> None:
        # #258 fix round 2, blocker 1.2 at the READ tier: a row storing a scalar
        # for a list-typed setting must be raised LOUD, never silently degraded
        # to ``[]`` (which would mask a corrupt override with no signal).
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("excluded_skills", value=True)
        with pytest.raises(ValueError, match="excluded_skills"):
            get_effective_settings()

    def test_list_row_resolves_canonical_list(self) -> None:
        # No-regression GREEN guard: a real stored list resolves to the canonical
        # coerced list.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("excluded_skills", ["foo", "bar"])
        assert get_effective_settings().excluded_skills == ["foo", "bar"]


class TestPerOverlayDbScope(TestCase):
    """Per-overlay scope in the DB override tier — global then overlay (later wins).

    The DB tier mirrors the TOML two-tier shape: a global ``ConfigSetting`` row
    (``scope=""``) applies to every overlay; an overlay-scoped row applies to
    that overlay alone and beats the global DB row, exactly as a per-overlay
    ``[overlays.<name>]`` TOML value beats the global ``[teatree]`` value.

    Integration-first: real TOML under ``tmp_path``, the active overlay set via
    ``T3_OVERLAY_NAME``, asserted through the real ``get_effective_settings``.
    """

    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        # A registered overlay so T3_OVERLAY_NAME resolves to an active overlay.
        _write_toml(
            self.config_path,
            '[teatree]\nissue_implementer_enabled = false\n\n[overlays.my-overlay]\nclass = "x.y:Z"\n',
        )
        self.monkeypatch = monkeypatch

    def test_overlay_scoped_db_row_beats_global_db_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)  # global
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        # Active overlay = my-overlay -> overlay-scoped True wins over global False.
        assert get_effective_settings().issue_implementer_enabled is True

    def test_overlay_scoped_db_row_ignored_for_a_different_overlay(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)  # global
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        # No active overlay (or a different one) -> only the global row applies.
        assert get_effective_settings().issue_implementer_enabled is False
        # And an explicit named-overlay resolution for another overlay ignores it.
        assert get_effective_settings("another").issue_implementer_enabled is False

    def test_global_db_row_applies_when_overlay_has_no_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)  # global only
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        # The overlay has no scoped row -> the global DB row still applies.
        assert get_effective_settings().issue_implementer_enabled is True

    def test_overlay_scoped_row_resolves_through_named_overlay_path(self) -> None:
        # The loop's per-overlay scanners call get_effective_settings(overlay_name);
        # that path must read the overlay's DB scope too (no env applied there).
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)  # global
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        assert get_effective_settings("my-overlay").issue_implementer_enabled is True

    def test_env_still_wins_over_overlay_scoped_db_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        self.monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "true")
        # env is the highest tier — it beats an overlay-scoped DB row too.
        assert get_effective_settings().issue_implementer_enabled is True

    def test_overlay_scope_matches_canonical_alias(self) -> None:
        # A row stored under the t3- entry-point spelling resolves for the short
        # alias active overlay (and vice versa) — same canonical fold as TOML.
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="t3-my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().issue_implementer_enabled is True

    def test_empty_overlay_scope_is_still_a_no_op(self) -> None:
        # The per-overlay extension keeps the empty-table no-op invariant: with no
        # rows at all, an active overlay resolves to the file value unchanged.
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().issue_implementer_enabled is False
