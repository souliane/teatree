"""One line saying what a setting accepts, derived from the schema and never hand-listed.

The failure this guards: an operator reading a ``config_setting export`` dump can see that
``wip`` is ``"full"`` but not whether it takes any string or one of four words. The answer
already exists (:func:`~teatree.config.schema.setting_choices`, which the dashboard's
selects are built from); these assertions pin it reaching the text surface intact, and
totally — every schema key resolves a type, so a new setting cannot ship unannotated.
"""

import pytest

from teatree.config.schema import TeatreeSettingsSchema, setting_choices
from teatree.config.setting_annotation import choice_token, setting_annotation, setting_type_name

_MAX_REPORTED = 8


class TestTheTypeNameIsDerivedFromTheAnnotation:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("autoload", "bool"),
            ("agent_max_turns", "int"),
            ("disk_crit_free_gb", "float"),
            ("agent_harness", "str"),
            ("clean_ignore", "list"),
            ("mr_reminder", "table"),
            ("wip", "str"),  # a StrEnum is stored as the str it subclasses
            ("repo_mode", "str"),  # a Literal is named by its members' shared type
            ("agent_harness_provider", "str"),  # an optional enum is named beside its None
        ],
    )
    def test_a_representative_key_of_each_shape_names_its_stored_type(self, key: str, expected: str) -> None:
        assert setting_type_name(key) == expected

    def test_a_bool_is_not_named_int_despite_subclassing_it(self) -> None:
        # The exact-type lookup exists for this: `issubclass(bool, int)` holds, so a
        # subclass walk would name all 104 booleans `int`.
        assert setting_type_name("autoload") == "bool"

    def test_every_schema_key_resolves_a_type(self) -> None:
        unnamed = sorted(key for key in TeatreeSettingsSchema.model_fields if not setting_type_name(key))
        assert not unnamed, f"{len(unnamed)} setting(s) resolve no type name: {unnamed[:_MAX_REPORTED]}"

    def test_a_key_the_schema_does_not_declare_resolves_to_empty_rather_than_raising(self) -> None:
        assert setting_type_name("no-such-setting") == ""


class TestTheChoiceTokenMakesTheInvisibleValuesVisible:
    def test_a_plain_value_reads_as_itself(self) -> None:
        assert choice_token("headless") == "headless"

    def test_the_empty_string_reads_as_a_visible_pair_of_quotes(self) -> None:
        # `privacy` and `repo_mode` both admit "" as a real state (auto-detect / unset);
        # rendered bare it would vanish into the separators around it.
        assert choice_token("") == '""'

    def test_none_reads_as_a_word_rather_than_a_hole(self) -> None:
        assert choice_token(None) == "none"


class TestTheAnnotationSaysWhatMayIPutHere:
    def test_an_open_typed_key_names_its_type_alone(self) -> None:
        assert setting_annotation("agent_max_turns") == "int"

    def test_a_constrained_key_names_its_alternatives(self) -> None:
        assert setting_annotation("wip") == "str, one of: slow | medium | full | boost"

    def test_a_bool_names_its_type_and_no_list(self) -> None:
        # Its two values ARE the type; spelling `true | false` beside 104 keys is noise.
        assert setting_annotation("autoload") == "bool"

    def test_a_key_the_schema_does_not_declare_is_annotated_with_nothing(self) -> None:
        assert setting_annotation("no-such-setting") == ""

    def test_every_constrained_key_offers_every_value_the_schema_admits(self) -> None:
        # Total over the schema rather than a sample: a derivation that dropped a member
        # for one key would otherwise ship an option list the validator disagrees with.
        for key in TeatreeSettingsSchema.model_fields:
            choices = setting_choices(key)
            if not choices or choices == (True, False):
                continue
            offered = setting_annotation(key).split("one of:", 1)[1]
            missing = [value for value in choices if choice_token(value) not in offered]
            assert not missing, f"{key}: {missing} missing from {offered!r}"
