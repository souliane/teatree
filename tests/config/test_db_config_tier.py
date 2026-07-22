# test-path: cross-cutting
"""DB-home settings in the effective-settings resolution chain (#1775 partition).

A DB-home field's SOLE source is the ``ConfigSetting`` store (global + overlay
rows) plus the ``T3_*`` env layer (which still wins). An empty table resolves the
dataclass default. Per DB-home field:

    env -> ConfigSetting (overlay then global) -> dataclass default

Pilot setting: ``orchestrate_claim_enabled`` (a boolean opt-in gate, default
``False``) so an EMPTY table is a provable no-op and the precedence is observable.

Integration-first: real ``ConfigSetting`` rows against the real DB, the active
overlay set via ``T3_OVERLAY_NAME``.
"""

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


class TestDbConfigTier(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ORCHESTRATE_CLAIM_ENABLED", raising=False)
        self.monkeypatch = monkeypatch

    def test_empty_table_is_a_no_op(self) -> None:
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().orchestrate_claim_enabled is False

    def test_db_is_the_sole_source_for_a_db_home_field(self) -> None:
        assert get_effective_settings().orchestrate_claim_enabled is False
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True)
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_db_row_is_the_sole_overlay_source(self) -> None:
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is False
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_env_wins_over_db_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        self.monkeypatch.setenv("T3_ORCHESTRATE_CLAIM_ENABLED", "true")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_db_row_for_non_overridable_key_is_ignored(self) -> None:
        # The pilot is scoped to OVERLAY_OVERRIDABLE_SETTINGS so an unknown /
        # non-overridable key never silently mutates the resolved settings.
        ConfigSetting.objects.set_value("not_a_real_setting", "boom")
        assert get_effective_settings().orchestrate_claim_enabled is False

    def test_db_row_value_is_coerced_via_registry_parser(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", "5")
        assert get_effective_settings().issue_implementer_max_concurrent == 5

    def test_clear_restores_dataclass_default(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True)
        assert get_effective_settings().orchestrate_claim_enabled is True
        ConfigSetting.objects.clear("orchestrate_claim_enabled")
        assert get_effective_settings().orchestrate_claim_enabled is False

    def test_bool_row_false_resolves_false(self) -> None:
        # #258 blocker 2: a stored real-bool ``False`` for an opt-in safety
        # setting must resolve to Python ``False`` — never truthy-coerced on.
        ConfigSetting.objects.set_value("allow_destructive_disk", value=False)
        assert get_effective_settings().allow_destructive_disk is False

    def test_quoted_bool_string_row_does_not_silently_enable(self) -> None:
        # #258 blocker 2 at the READ tier: a row storing the JSON STRING ``"false"``
        # must NOT silently enable the opt-in setting via ``bool("false") == True``.
        ConfigSetting.objects.set_value("allow_destructive_disk", "false")
        with pytest.raises(ValueError, match="allow_destructive_disk"):
            get_effective_settings()

    def test_bool_row_for_int_setting_is_rejected_loud(self) -> None:
        # #258 round 2: a row storing JSON ``true`` for an int-typed setting is
        # raised LOUD with the offending key, never coerced via ``int(True) == 1``.
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", value=True)
        with pytest.raises(ValueError, match="issue_implementer_max_concurrent"):
            get_effective_settings()

    def test_scalar_row_for_list_setting_is_rejected_loud(self) -> None:
        # #258 round 2: a scalar row for a list-typed setting is raised LOUD, never
        # silently degraded to ``[]`` (which would mask a corrupt override).
        ConfigSetting.objects.set_value("excluded_skills", value=True)
        with pytest.raises(ValueError, match="excluded_skills"):
            get_effective_settings()

    def test_list_row_resolves_canonical_list(self) -> None:
        ConfigSetting.objects.set_value("excluded_skills", ["foo", "bar"])
        assert get_effective_settings().excluded_skills == ["foo", "bar"]


class TestPerOverlayDbScope(TestCase):
    """Per-overlay scope in the DB override tier — global then overlay (later wins).

    A global ``ConfigSetting`` row (``scope=""``) applies to every overlay; an
    overlay-scoped row applies to that overlay alone and beats the global DB row.
    The active overlay is set via ``T3_OVERLAY_NAME``.
    """

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ORCHESTRATE_CLAIM_ENABLED", raising=False)
        self.monkeypatch = monkeypatch

    def test_overlay_scoped_db_row_beats_global_db_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_scoped_db_row_ignored_for_a_different_overlay(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is False
        assert get_effective_settings("another").orchestrate_claim_enabled is False

    def test_global_db_row_applies_when_overlay_has_no_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True)
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_scoped_row_resolves_through_named_overlay_path(self) -> None:
        # The loop's per-overlay scanners call get_effective_settings(overlay_name);
        # that path must read the overlay's DB scope too (no env applied there).
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        assert get_effective_settings("my-overlay").orchestrate_claim_enabled is True

    def test_env_still_wins_over_overlay_scoped_db_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        self.monkeypatch.setenv("T3_ORCHESTRATE_CLAIM_ENABLED", "true")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_scope_matches_canonical_alias(self) -> None:
        # A row stored under the t3- entry-point spelling resolves for the short
        # alias active overlay (and vice versa).
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="t3-my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_empty_overlay_scope_is_still_a_no_op(self) -> None:
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().orchestrate_claim_enabled is False
