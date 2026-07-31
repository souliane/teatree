# test-path: cross-cutting
"""The help table is TOTAL over the schema, and shaped for the surfaces that render it.

Help text is authored once (:mod:`teatree.config.setting_help`) and rendered twice — as the
``defaults.toml`` comment above a key and as the dashboard tooltip on it. Totality is what
makes "authored once" hold: a new setting with no entry fails here rather than shipping an
unexplained key to both surfaces.
"""

from django.test import SimpleTestCase

from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_help import SETTING_HELP, setting_help

_MAX_REPORTED = 8


class TestHelpCoversTheSchema(SimpleTestCase):
    def test_every_schema_key_has_help_text(self) -> None:
        missing = sorted(set(TeatreeSettingsSchema.model_fields) - set(SETTING_HELP))
        assert not missing, f"{len(missing)} setting(s) carry no help text: {missing[:_MAX_REPORTED]}"

    def test_the_table_names_no_key_the_schema_does_not_declare(self) -> None:
        stale = sorted(set(SETTING_HELP) - set(TeatreeSettingsSchema.model_fields))
        assert not stale, f"{len(stale)} help entr(ies) name no schema key: {stale[:_MAX_REPORTED]}"

    def test_no_entry_is_blank(self) -> None:
        blank = sorted(key for key, text in SETTING_HELP.items() if not text.strip())
        assert not blank, f"blank help text: {blank[:_MAX_REPORTED]}"


class TestHelpIsShapedForBothSurfaces(SimpleTestCase):
    def test_no_entry_carries_the_toml_key_value_separator(self) -> None:
        # The shipped-file conformance suite reads keys out of the rendered TOML by splitting
        # on " =", so a help comment carrying one would be counted as a settings line.
        offenders = sorted(key for key, text in SETTING_HELP.items() if " =" in text)
        assert not offenders, f"help text carries the TOML separator: {offenders[:_MAX_REPORTED]}"

    def test_no_entry_spans_more_than_one_line(self) -> None:
        # A TOML comment is one line; a second line would render outside the comment.
        offenders = sorted(key for key, text in SETTING_HELP.items() if "\n" in text)
        assert not offenders, f"help text spans several lines: {offenders[:_MAX_REPORTED]}"


class TestLookup(SimpleTestCase):
    def test_a_known_key_resolves_its_sentence(self) -> None:
        assert setting_help("merge_wip") == SETTING_HELP["merge_wip"]

    def test_an_unknown_key_resolves_to_empty_rather_than_raising(self) -> None:
        assert setting_help("no-such-setting") == ""
