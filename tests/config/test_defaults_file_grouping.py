# test-path: cross-cutting
"""The shipped ``[teatree]`` block reads as the group tree, and nesting it moved no value.

``defaults.toml`` is hand-edited, and its ``[teatree]`` table used to be ~200 consecutive
flat ``key = value`` lines. Rendered by :func:`grouped_settings_table`, the declaration
hierarchy is real nested TOML tables — the same tree the dashboard renders and the export
dump emits: one walk, three surfaces.

Two failure modes are guarded. A hand-edit that appends a key at the bottom of the block
breaks the order conformance, and the message names the group the key belongs to. And the
whole exercise is a pure re-shaping: the nested table must flatten to exactly what the same
rows parse to when rendered flat, so no value can ride along with the move — the
nested-vs-flat parse-equality this ticket's file-shape change rests on.
"""

import tomllib
from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.schema import _DEFAULTS_TOML
from teatree.config.setting_groups import grouped_key_order, grouped_settings_table, setting_group_path

_MAX_REPORTED = 5
_SUBTABLE_KEYS = ("mr_reminder", "speak")


def _file_text() -> str:
    return _DEFAULTS_TOML.read_text(encoding="utf-8")


def _teatree_block() -> str:
    """The GROUP region of ``[teatree]`` — the nested wrappers, before the sub-table settings.

    ``speak`` / ``mr_reminder`` are declared settings whose value is a table, so they render
    after the wrappers; their inner keys are values, not settings, and are excluded here.
    """
    section = _file_text()[_file_text().index("\n[teatree.") :]
    return section[: section.index(f"\n[teatree.{_SUBTABLE_KEYS[0]}]")]


def _flat_keys_in_file_order() -> tuple[str, ...]:
    return tuple(line.split(" =")[0] for line in _teatree_block().splitlines() if " = " in line)


def _group_headers(text: str) -> tuple[str, ...]:
    """Every ``[teatree.<group>…]`` header in *text*, excluding the sub-table settings' own."""
    return tuple(
        line
        for line in text.splitlines()
        if line.startswith("[teatree.") and not any(f"[teatree.{key}" in line for key in _SUBTABLE_KEYS)
    )


def _shipped_rows() -> dict[str, Any]:
    return flatten_settings_table(tomllib.loads(_file_text())["teatree"])


def _dumped(table: tomlkit_items.Table) -> str:
    document = tomlkit.document()
    document["teatree"] = table
    return tomlkit.dumps(document)


def _flat_rendering(rows: dict[str, Any]) -> tomlkit_items.Table:
    flat = tomlkit.table()
    for key in sorted(rows):
        flat[key] = rows[key]
    return flat


def _misplacement_report(actual: tuple[str, ...], expected: tuple[str, ...]) -> str:
    misplaced = [key for key, wanted in zip(actual, expected, strict=True) if key != wanted]
    lines = [
        f"  {key} belongs under `{' > '.join(setting_group_path(key))}`,"
        f" immediately after `{expected[expected.index(key) - 1]}`"
        if expected.index(key)
        else f"  {key} belongs first in the block, under `{' > '.join(setting_group_path(key))}`"
        for key in misplaced[:_MAX_REPORTED]
    ]
    return (
        f"the shipped `[teatree]` block is not in group order — {len(misplaced)} key(s) sit outside "
        f"their group's table. Move each one under the table named below "
        f"(the group comes from the `UserSettings` base that declares the key):\n" + "\n".join(lines)
    )


class TestTheShippedBlockIsNested:
    def test_the_key_order_is_the_group_walk_not_the_alphabet(self) -> None:
        actual = _flat_keys_in_file_order()
        assert set(actual) == set(_shipped_rows()) - set(_SUBTABLE_KEYS), "the block parser missed a key"
        expected = grouped_key_order(actual)
        assert actual == expected, _misplacement_report(actual, expected)
        assert actual != tuple(sorted(actual)), "the block collapsed back to one flat alphabetical wall"

    def test_every_group_is_a_real_table_path_not_a_comment_banner(self) -> None:
        rendered = _group_headers(_dumped(grouped_settings_table(_shipped_rows())))
        assert _group_headers(_file_text()) == rendered
        assert not [line for line in _teatree_block().splitlines() if line.startswith("#")]

    def test_the_hierarchy_is_visible_several_levels_deep(self) -> None:
        depths = {header.count(".") for header in _group_headers(_file_text())}
        assert max(depths) >= 3, f"the tables are not nested several levels deep: depths={sorted(depths)}"

    def test_a_genuine_sub_table_setting_stays_at_its_own_top_level_path(self) -> None:
        # ``speak`` and ``mr_reminder`` are declared settings whose value IS a table. They
        # are not group wrappers, so they keep the paths every reader already knows.
        for key in _SUBTABLE_KEYS:
            assert f"\n[teatree.{key}]\n" in _file_text()


class TestNestingMovedNoValue:
    def test_the_nested_block_flattens_to_exactly_what_a_flat_rendering_parses_to(self) -> None:
        rows = _shipped_rows()
        nested = tomllib.loads(_dumped(grouped_settings_table(rows)))["teatree"]
        assert flatten_settings_table(nested) == tomllib.loads(_dumped(_flat_rendering(rows)))["teatree"]

    def test_the_shipped_file_parses_to_the_same_rows_a_flat_rendering_would(self) -> None:
        rows = _shipped_rows()
        assert rows == tomllib.loads(_dumped(_flat_rendering(rows)))["teatree"]

    def test_a_flat_hand_written_block_still_reads_identically(self) -> None:
        # The flattener is total over both shapes, so an operator's older flat export and
        # the nested shipped file resolve to the same mapping — nesting is a file shape.
        rows = _shipped_rows()
        flat_text = _dumped(_flat_rendering(rows))
        assert flatten_settings_table(tomllib.loads(flat_text)["teatree"]) == rows
