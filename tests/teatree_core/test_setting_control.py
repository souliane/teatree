"""The ONE per-setting derivation both the editor grid and the snapshot compose.

The value of factoring it out is that there is no second answer to derive; these assertions
pin the answers that a second copy would drift on — the kind rule, the masking rule, and the
options being the schema's own admissible set rather than a hand-kept list.
"""

from django.test import SimpleTestCase

from teatree.config.schema import TeatreeSettingsSchema, setting_choices
from teatree.config.setting_help import setting_help
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.core.config_display import MASKED, NO_SHIPPED_DEFAULT
from teatree.core.setting_control import SettingControl, annotation_text, setting_kind, value_kind, wire

#: A boolean bound to a name, so a control-vocabulary assertion reads as a VALUE under test.
_ON = True


class TestKindDerivation(SimpleTestCase):
    def test_a_boolean_is_a_bool_not_an_enum_even_though_it_has_two_choices(self) -> None:
        assert setting_kind(bool, (True, False)) == "bool"

    def test_a_constrained_string_is_an_enum(self) -> None:
        assert setting_kind(str, ("a", "b")) == "enum"

    def test_an_open_valued_type_is_named_by_its_python_type(self) -> None:
        assert setting_kind(str, ()) == "str"
        assert setting_kind(int, ()) == "int"
        assert setting_kind(dict[str, str], ()) == "dict"

    def test_every_collection_type_collapses_to_list(self) -> None:
        assert [setting_kind(t, ()) for t in (list[str], tuple[str], set[str], frozenset[str])] == ["list"] * 4

    def test_an_optional_wrapper_is_unwrapped(self) -> None:
        assert setting_kind(bool | None, ()) == "bool"

    def test_an_undeclared_annotation_is_unknown_rather_than_a_guess(self) -> None:
        assert setting_kind(object, ()) == "unknown"

    def test_an_observed_value_is_named_by_its_own_type(self) -> None:
        assert value_kind(_ON) == "bool"
        assert value_kind([1, 2]) == "list"
        assert value_kind(object()) == "unknown"


class TestAnnotationText(SimpleTestCase):
    def test_a_plain_type_reads_as_its_name(self) -> None:
        assert annotation_text(str) == "str"

    def test_no_annotation_reads_as_empty_rather_than_none(self) -> None:
        assert annotation_text(None) == ""


class TestControlDerivesFromTheSchemaAlone(SimpleTestCase):
    def test_every_schema_key_derives_a_control_without_raising(self) -> None:
        for key in TeatreeSettingsSchema.model_fields:
            control = SettingControl(key)
            assert control.name == key
            assert control.kind
            assert isinstance(control.help_text, str)

    def test_help_text_is_the_authored_sentence_not_a_second_one(self) -> None:
        assert SettingControl("merge_wip").help_text == setting_help("merge_wip")

    def test_choices_are_the_schemas_own_admissible_set(self) -> None:
        key = next(k for k in TeatreeSettingsSchema.model_fields if setting_choices(k))
        assert [choice.value for choice in SettingControl(key).choices] == [
            wire(value) for value in setting_choices(key)
        ]

    def test_an_open_valued_key_offers_no_options_so_a_surface_renders_free_text(self) -> None:
        key = next(k for k in TeatreeSettingsSchema.model_fields if not setting_choices(k))
        assert SettingControl(key).choices == ()

    def test_the_safety_posture_verdict_is_the_registry_not_a_second_list(self) -> None:
        key = next(iter(sorted(SAFETY_POSTURE_KEYS)))
        assert SettingControl(key).is_safety_posture

    def test_an_unknown_key_derives_a_control_rather_than_raising(self) -> None:
        control = SettingControl("no-such-setting")
        assert control.kind == "unknown"
        assert control.annotation_text == ""


class TestShippedDefaultRendering(SimpleTestCase):
    def test_a_key_the_file_carries_renders_its_value(self) -> None:
        control = SettingControl("merge_wip", {"merge_wip": True})
        assert control.has_shipped_default
        assert control.shipped_default == "on"

    def test_a_key_the_file_does_not_carry_says_so_rather_than_reading_as_a_value(self) -> None:
        control = SettingControl("merge_wip", {})
        assert not control.has_shipped_default
        assert control.shipped_default == NO_SHIPPED_DEFAULT


class TestMaskingIsOneDecisionAppliedToBothRenderings(SimpleTestCase):
    def _secret_key(self) -> str:
        return next(key for key in TeatreeSettingsSchema.model_fields if SettingControl(key).is_secret)

    def test_a_secret_key_masks_its_display_value(self) -> None:
        assert SettingControl(self._secret_key()).display_value("hunter2") == MASKED

    def test_a_secret_key_masks_its_wire_value_too_so_no_control_can_hold_it(self) -> None:
        assert SettingControl(self._secret_key()).wire_value("hunter2") == MASKED

    def test_a_secret_keys_shipped_default_is_masked_as_well(self) -> None:
        key = self._secret_key()
        assert SettingControl(key, {key: "hunter2"}).shipped_default == MASKED

    def test_a_plain_key_renders_its_real_value_on_both(self) -> None:
        control = SettingControl("merge_wip")
        assert control.display_value(_ON) == "on"
        assert control.wire_value(_ON) == "true"
