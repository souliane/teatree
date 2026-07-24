"""DB-backed config override store (souliane/teatree#1775, first slice).

Integration-first against the real DB: the ``ConfigSetting`` key/value row
is the canonical override tier (mirrors ``MergeClear`` / ``DbApproval`` —
"canonical tier is the DB with file/env fallback"). An absent key resolves to
``None`` so the resolver falls through to the file/env source; a present row
returns its stored value.
"""

import pytest
from django.core.exceptions import ValidationError
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ENTRYPOINT_SEEDER, GLOBAL_SCOPE, SeedOutcome


class TestConfigSettingStore(TestCase):
    def test_get_effective_absent_key_is_none(self) -> None:
        # Empty table -> fall-through sentinel, never an exception.
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is None

    def test_set_value_then_get_effective_returns_it(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True

    def test_set_value_is_an_upsert_on_unique_key(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        assert ConfigSetting.objects.filter(key="issue_implementer_enabled").count() == 1
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False

    def test_value_round_trips_non_bool_json(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "ready-to-implement")
        assert ConfigSetting.objects.get_effective("issue_implementer_label") == "ready-to-implement"

    def test_value_round_trips_int_and_list(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 3)
        ConfigSetting.objects.set_value("excluded_skills", ["a", "b"])
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent") == 3
        assert ConfigSetting.objects.get_effective("excluded_skills") == ["a", "b"]

    def test_clear_removes_the_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        removed = ConfigSetting.objects.clear("issue_implementer_enabled")
        assert removed is True
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is None

    def test_clear_absent_key_returns_false(self) -> None:
        assert ConfigSetting.objects.clear("never_set") is False

    def test_str_is_informative(self) -> None:
        row = ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert "issue_implementer_enabled" in str(row)


class TestConfigSettingEmptyValuesRoundTrip(TestCase):
    """``[]``/``{}`` survive the manager's write→read path, not just ``full_clean``.

    ``TestConfigSettingValueValidation`` covers the form/validation layer; these
    prove an empty override actually comes back out of the store, so a caller
    reading ``statusline_chain`` gets the override rather than the shipped default.
    """

    def test_empty_list_round_trips(self) -> None:
        ConfigSetting.objects.set_value("statusline_chain", [])
        assert ConfigSetting.objects.get_effective("statusline_chain") == []

    def test_empty_dict_round_trips(self) -> None:
        ConfigSetting.objects.set_value("agent_skill_models", {})
        assert ConfigSetting.objects.get_effective("agent_skill_models") == {}


class TestConfigSettingScope(TestCase):
    """Per-overlay scope on the DB override tier (per-overlay + global).

    A global row (``scope=""``) and an overlay-scoped row for the same key are
    distinct rows; the manager reads/writes/clears within a scope; ``set_value``
    upserts per scope.
    """

    def test_global_and_overlay_rows_for_same_key_coexist(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        # Two distinct rows for one key — the composite (scope, key) uniqueness.
        assert ConfigSetting.objects.filter(key="issue_implementer_enabled").count() == 2
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled", scope="my-overlay") is True

    def test_set_value_is_per_scope_upsert(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 1, scope="ov")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 2, scope="ov")
        assert ConfigSetting.objects.filter(key="issue_implementer_max_concurrent", scope="ov").count() == 1
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent", scope="ov") == 2

    def test_overlay_get_absent_falls_to_none_not_global(self) -> None:
        # An overlay read never silently borrows the global row — absence in the
        # overlay scope is the None fall-through sentinel; the resolver, not the
        # manager, layers global-then-overlay.
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled", scope="other") is None

    def test_clear_is_scope_isolated(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="ov")
        assert ConfigSetting.objects.clear("issue_implementer_enabled", scope="ov") is True
        # The global row survives an overlay-scoped clear.
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled", scope="ov") is None

    def test_overrides_for_scope_returns_only_that_scope(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        ConfigSetting.objects.set_value("issue_implementer_label", "ready", scope="ov")
        assert ConfigSetting.objects.overrides_for_scope("") == {"issue_implementer_enabled": True}
        assert ConfigSetting.objects.overrides_for_scope("ov") == {"issue_implementer_label": "ready"}

    def test_str_names_overlay_scope(self) -> None:
        row = ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        assert "my-overlay" in str(row)


class TestSeedProvenance(TestCase):
    """Provenance-aware deploy seed (#3435).

    A changed shipped default reaches an existing box, a code-default seed is
    never written, and an operator override is always preserved.
    """

    def test_seed_below_default_creates_with_provenance(self) -> None:
        outcome = ConfigSetting.objects.seed("provision_max_concurrency", 1, code_default=0)
        assert outcome is SeedOutcome.CREATED
        row = ConfigSetting.objects.get(key="provision_max_concurrency")
        assert row.value == 1
        assert row.seed_value == 1
        assert row.seeded_by == ENTRYPOINT_SEEDER

    def test_seed_equal_to_code_default_is_not_written(self) -> None:
        outcome = ConfigSetting.objects.seed("provision_max_concurrency", 0, code_default=0)
        assert outcome is SeedOutcome.SKIPPED_DEFAULT
        assert ConfigSetting.objects.filter(key="provision_max_concurrency").exists() is False

    def test_reseed_same_value_is_a_noop(self) -> None:
        ConfigSetting.objects.seed("provision_ram_ceiling_percent", 75, code_default=85)
        outcome = ConfigSetting.objects.seed("provision_ram_ceiling_percent", 75, code_default=85)
        assert outcome is SeedOutcome.UNCHANGED
        assert ConfigSetting.objects.get_effective("provision_ram_ceiling_percent") == 75

    def test_changed_shipped_seed_reseeds_an_unchanged_row(self) -> None:
        # An old deploy seeded 70; the row still equals that seed (operator never
        # touched it), so a new deploy shipping 75 must update it.
        ConfigSetting.objects.seed("provision_ram_ceiling_percent", 70, code_default=85)
        outcome = ConfigSetting.objects.seed("provision_ram_ceiling_percent", 75, code_default=85)
        assert outcome is SeedOutcome.UPDATED
        row = ConfigSetting.objects.get(key="provision_ram_ceiling_percent")
        assert row.value == 75
        assert row.seed_value == 75

    def test_operator_override_is_preserved_across_reseed(self) -> None:
        ConfigSetting.objects.seed("provision_ram_ceiling_percent", 70, code_default=85)
        # The operator pins their own value via the admin `set` path.
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 90)
        outcome = ConfigSetting.objects.seed("provision_ram_ceiling_percent", 75, code_default=85)
        assert outcome is SeedOutcome.PRESERVED
        assert ConfigSetting.objects.get_effective("provision_ram_ceiling_percent") == 90

    def test_reseed_equal_to_default_removes_an_owned_row(self) -> None:
        # A row this seeder owns whose shipped seed now equals the code default is
        # DROPPED so the live code default flows through (never frozen).
        ConfigSetting.objects.seed("provision_ram_ceiling_percent", 70, code_default=85)
        outcome = ConfigSetting.objects.seed("provision_ram_ceiling_percent", 85, code_default=85)
        assert outcome is SeedOutcome.REMOVED
        assert ConfigSetting.objects.filter(key="provision_ram_ceiling_percent").exists() is False

    def test_row_seeded_by_another_marker_is_preserved(self) -> None:
        ConfigSetting.objects.seed("provision_ram_ceiling_percent", 70, code_default=85, seeded_by="other")
        outcome = ConfigSetting.objects.seed("provision_ram_ceiling_percent", 75, code_default=85)
        assert outcome is SeedOutcome.PRESERVED
        assert ConfigSetting.objects.get_effective("provision_ram_ceiling_percent") == 70

    def test_set_value_clears_seed_provenance(self) -> None:
        ConfigSetting.objects.seed("provision_ram_ceiling_percent", 75, code_default=85)
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 75)
        row = ConfigSetting.objects.get(key="provision_ram_ceiling_percent")
        assert row.seeded_by == ""
        assert row.seed_value is None


class TestConfigSettingValueValidation(TestCase):
    """``full_clean`` accepts every legitimately-empty JSON value but refuses ``None``.

    Any ``ModelForm`` over this store (the Django admin included) resolves the
    empty-vs-required question here, so the model — not a per-form override —
    is what keeps ``[]`` savable and a blank submission out of the NOT NULL
    column.
    """

    def test_full_clean_accepts_legitimately_empty_values(self) -> None:
        for value in ([], {}, "", 0, False):
            ConfigSetting(scope=GLOBAL_SCOPE, key="statusline_chain", value=value).full_clean()

    def test_full_clean_rejects_none_as_a_value_field_error(self) -> None:
        row = ConfigSetting(scope=GLOBAL_SCOPE, key="statusline_chain", value=None)
        with pytest.raises(ValidationError) as caught:
            row.full_clean()
        assert "value" in caught.value.message_dict
