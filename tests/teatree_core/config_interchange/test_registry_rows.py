"""The registry-row interchange rules, at the level the round-trip properties rest on.

``tests/teatree_core/test_config_round_trip.py`` asserts the properties an operator sees —
a redacted export re-imports as no change, and applying it deletes nothing. These pin the
two rules that make those properties hold, including the shapes a hand-edited or
partly-migrated store can carry that a whole round trip never produces.
"""

from teatree.core.config_interchange.registry_rows import merged_registry, overlay_table_split


class TestMergedRegistry:
    """*incoming* over *stored*, entry by entry and field by field."""

    def test_a_field_the_file_omits_survives(self) -> None:
        merged = merged_registry({"demo": {"path": "~/demo"}}, {"demo": {"path": "~/old", "token_ref": "vault/demo"}})
        assert merged == {"demo": {"path": "~/demo", "token_ref": "vault/demo"}}

    def test_an_entry_the_file_omits_survives(self) -> None:
        assert merged_registry({"a": {"path": "/a"}}, {"b": {"path": "/b"}}) == {
            "a": {"path": "/a"},
            "b": {"path": "/b"},
        }

    def test_it_does_not_mutate_the_stored_value(self) -> None:
        stored = {"demo": {"path": "~/old"}}
        merged_registry({"demo": {"path": "~/new"}}, stored)
        assert stored == {"demo": {"path": "~/old"}}

    def test_no_stored_row_leaves_the_file_as_the_whole_answer(self) -> None:
        assert merged_registry({"demo": {"path": "/d"}}, None) == {"demo": {"path": "/d"}}

    def test_a_non_table_entry_on_either_side_is_replaced_not_merged(self) -> None:
        # There is no field-wise merge to do when one side is not a table, and guessing one
        # would invent a shape neither the file nor the store ever held.
        assert merged_registry({"demo": {"path": "/d"}}, {"demo": "legacy"}) == {"demo": {"path": "/d"}}
        assert merged_registry({"demo": "legacy"}, {"demo": {"path": "/d"}}) == {"demo": "legacy"}

    def test_a_non_table_stored_row_is_carried_through_untouched(self) -> None:
        assert merged_registry({}, {"demo": "legacy"}) == {"demo": "legacy"}
        assert merged_registry({"other": {"path": "/o"}}, "not-a-table") == {"other": {"path": "/o"}}


class TestOverlayTableSplit:
    """One ``[overlays.<name>]`` table back into the two things the export joined."""

    def test_declared_settings_and_definition_keys_part_ways(self) -> None:
        settings, definitions = overlay_table_split(
            {"path": "~/demo", "class": "x.Y", "mode": "auto", "agent_phase_harness": {"coding": "codex"}}
        )
        assert settings == {"mode": "auto", "agent_phase_harness": {"coding": "codex"}}
        assert definitions == {"path": "~/demo", "class": "x.Y"}

    def test_a_definition_only_table_yields_no_setting_rows(self) -> None:
        assert overlay_table_split({"path": "~/demo"}) == ({}, {"path": "~/demo"})
