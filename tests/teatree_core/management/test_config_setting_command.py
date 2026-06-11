"""``t3 <overlay> config-setting`` admin path for the DB override tier (#1775).

The management command is the sanctioned way to set/clear a ``ConfigSetting``
row (the ORM-touching admin path). Integration-first via ``call_command``
against the real DB; the value is parsed as JSON so a bool kill-switch, a
string, an int, or a list all round-trip into the override store.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.config.enums import Mode
from teatree.core.models import ConfigSetting


class TestConfigSettingSet(TestCase):
    def test_set_bool_creates_row(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True

    def test_set_is_upsert(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        call_command("config_setting", "set", "issue_implementer_enabled", "false")
        assert ConfigSetting.objects.filter(key="issue_implementer_enabled").count() == 1
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False

    def test_set_string_value(self) -> None:
        call_command("config_setting", "set", "issue_implementer_label", '"ready"')
        assert ConfigSetting.objects.get_effective("issue_implementer_label") == "ready"

    def test_set_int_value(self) -> None:
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "3")
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent") == 3

    def test_set_rejects_non_overridable_key(self) -> None:
        # Out of scope of the pilot: only OVERLAY_OVERRIDABLE_SETTINGS keys are
        # accepted so the admin cannot stash a row the resolver would ignore.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "not_a_real_setting", "true")
        assert ConfigSetting.objects.filter(key="not_a_real_setting").exists() is False

    def test_set_rejects_invalid_json(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "issue_implementer_enabled", "not-json")
        assert ConfigSetting.objects.filter(key="issue_implementer_enabled").exists() is False

    def test_set_rejects_out_of_enum_value_and_leaves_reads_working(self) -> None:
        # #258 blocker 1: a value that JSON-parses but is invalid for the
        # setting's type (an out-of-enum ``mode``) must be rejected at WRITE
        # time. Storing it would brick every config read — ``get_effective``'s
        # DB tier coerces each stored value via the registry parser, so a bad
        # ``mode`` row makes ``Mode.parse`` raise on EVERY resolution.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "mode", '"bogus"')
        assert ConfigSetting.objects.filter(key="mode").exists() is False
        # The store is untouched, so config reads still resolve.
        assert get_effective_settings().mode is not None

    def test_set_rejects_quoted_bool_string(self) -> None:
        # #258 blocker 2: a JSON string ``"false"`` for a bool-typed setting
        # must be rejected, not truthy-coerced via ``bool("false") == True``.
        # Silently enabling an opt-in safety setting is the failure mode.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "allow_destructive_disk", '"false"')
        assert ConfigSetting.objects.filter(key="allow_destructive_disk").exists() is False

    def test_set_accepts_real_json_bool_false(self) -> None:
        # The GREEN side of blocker 2: a real JSON boolean ``false`` resolves
        # to Python ``False`` and the opt-in setting stays disabled.
        call_command("config_setting", "set", "allow_destructive_disk", "false")
        assert ConfigSetting.objects.get_effective("allow_destructive_disk") is False

    def test_set_rejects_bool_for_int_setting(self) -> None:
        # #258 fix round 2, blocker 1.1: JSON ``true`` decodes to Python ``True``,
        # and ``int(True) == 1`` (bool is a subclass of int), so the lenient
        # ``int`` registry parser silently ACCEPTED a bool for an int-typed
        # setting and the raw ``True`` was persisted. The strict int parser must
        # REJECT a bool at WRITE time, leaving the store untouched.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "issue_implementer_max_concurrent", "true")
        assert ConfigSetting.objects.filter(key="issue_implementer_max_concurrent").exists() is False

    def test_set_rejects_scalar_for_list_setting(self) -> None:
        # #258 fix round 2, blocker 1.2: ``_parse_excluded_skills`` returned ``[]``
        # for ANY non-list scalar, so ``set excluded_skills true`` passed
        # validation and stored the raw ``True``. The strict list parser must
        # RAISE on a non-list scalar so the bad value is rejected at write time.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "excluded_skills", "true")
        assert ConfigSetting.objects.filter(key="excluded_skills").exists() is False

    def test_set_int_persists_canonical_value(self) -> None:
        # No-regression GREEN guard + canonical-value invariant: a JSON numeric
        # STRING ``"5"`` parses to the int ``5``, and the CANONICAL parsed value
        # (the int, not the raw ``"5"`` string) is persisted — so the DB row and
        # the read-time coercion agree on the int.
        call_command("config_setting", "set", "issue_implementer_max_concurrent", '"5"')
        row = ConfigSetting.objects.get(key="issue_implementer_max_concurrent")
        assert row.value == 5
        assert isinstance(row.value, int)
        assert get_effective_settings().issue_implementer_max_concurrent == 5

    def test_set_list_persists_canonical_value(self) -> None:
        # No-regression GREEN guard for blocker 1.2: a real JSON list is accepted
        # and stored as the canonical parsed list, readable back unchanged.
        call_command("config_setting", "set", "excluded_skills", '["foo"]')
        row = ConfigSetting.objects.get(key="excluded_skills")
        assert row.value == ["foo"]
        assert get_effective_settings().excluded_skills == ["foo"]

    def test_set_enum_persists_normalised_canonical_value(self) -> None:
        # The canonical-persistence change normalises an enum value: an UPPER-case
        # ``"AUTO"`` parses to ``Mode.AUTO`` whose ``StrEnum`` value is the
        # lower-case ``"auto"``. The CANONICAL (normalised) value is stored — not
        # the raw ``"AUTO"`` — so the row and the read tier agree, and the read
        # tier re-parses it to the same enum.
        call_command("config_setting", "set", "mode", '"AUTO"')
        row = ConfigSetting.objects.get(key="mode")
        assert row.value == "auto"
        assert get_effective_settings().mode is Mode.AUTO


class TestConfigSettingClear(TestCase):
    def test_clear_removes_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        call_command("config_setting", "clear", "issue_implementer_enabled")
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is None

    def test_clear_absent_key_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "clear", "never_set")


class TestConfigSettingList(TestCase):
    def test_list_shows_rows(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        assert "issue_implementer_enabled" in out.getvalue()

    def test_list_empty_is_clean(self) -> None:
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        assert "no" in out.getvalue().lower()
