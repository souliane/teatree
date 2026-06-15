"""DB-backed config override store (souliane/teatree#1775, first slice).

Integration-first against the real DB: the ``ConfigSetting`` key/value row
is the canonical override tier (mirrors ``MergeClear`` / ``DbApproval`` —
"canonical tier is the DB with file/env fallback"). An absent key resolves to
``None`` so the resolver falls through to the file/env source; a present row
returns its stored value.
"""

from django.test import TestCase

from teatree.core.models import ConfigSetting


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
