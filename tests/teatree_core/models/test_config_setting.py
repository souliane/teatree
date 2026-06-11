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
