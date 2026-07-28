# test-path: cross-cutting
"""The shipped ``[teatree]`` block reads as the group tree, and grouping it moved no value.

``defaults.toml`` is hand-edited, and its ``[teatree]`` table is ~200 consecutive flat
``key = value`` lines. Ordered by :func:`grouped_key_order` and banner-commented by
:func:`grouped_settings_table`, that wall reads with the same hierarchy the dashboard
renders and the export dump emits — one walk, three surfaces.

Two failure modes are guarded. A hand-edit that appends a key at the bottom of the block
breaks the order conformance, and the message names the group the key belongs to. And the
whole exercise is a pure reordering: the grouped table must parse to exactly what the
same rows parse to when rendered flat, so no value can ride along with the move.
"""

import tomllib
from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.schema import _DEFAULTS_TOML
from teatree.config.setting_groups import grouped_key_order, grouped_settings_table, setting_group_path

_MAX_REPORTED = 5
_SUBTABLE_KEYS = ("mr_reminder", "speak")


def _file_text() -> str:
    return _DEFAULTS_TOML.read_text(encoding="utf-8")


def _teatree_block() -> str:
    """The raw ``[teatree]`` section, up to its first sub-table."""
    section = _file_text()[_file_text().index("\n[teatree]\n") :]
    return section[: section.index("\n[teatree.")]


def _flat_keys_in_file_order() -> tuple[str, ...]:
    return tuple(line.split(" =")[0] for line in _teatree_block().splitlines() if " = " in line)


def _banner_comments() -> tuple[str, ...]:
    return tuple(line for line in _teatree_block().splitlines() if line.startswith("#"))


def _shipped_rows() -> dict[str, Any]:
    return tomllib.loads(_file_text())["teatree"]


def _dumped(table: tomlkit_items.Table) -> str:
    document = tomlkit.document()
    document["teatree"] = table
    return tomlkit.dumps(document)


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
        f"their group's banner. Move each one under the banner named below "
        f"(the group comes from the `UserSettings` base that declares the key):\n" + "\n".join(lines)
    )


class TestTheShippedBlockIsGrouped:
    def test_the_key_order_is_the_group_walk_not_the_alphabet(self) -> None:
        actual = _flat_keys_in_file_order()
        assert set(actual) == set(_shipped_rows()) - set(_SUBTABLE_KEYS), "the block parser missed a key"
        expected = grouped_key_order(actual)
        assert actual == expected, _misplacement_report(actual, expected)
        assert actual != tuple(sorted(actual)), "the block collapsed back to one flat alphabetical wall"

    def test_every_group_is_announced_by_an_indented_banner_comment(self) -> None:
        rows = {key: value for key, value in _shipped_rows().items() if key not in _SUBTABLE_KEYS}
        rendered = [line for line in _dumped(grouped_settings_table(rows)).splitlines() if line.startswith("#")]
        assert list(_banner_comments()) == rendered

    def test_the_hierarchy_is_visible_several_levels_deep(self) -> None:
        indents = {len(line) - len(line.lstrip("# ")) for line in _banner_comments()}
        assert len(indents) >= 3, f"the banners are not nested several levels deep: indents={sorted(indents)}"


class TestGroupingMovedNoValue:
    def test_the_grouped_block_parses_identically_to_a_flat_rendering(self) -> None:
        rows = _shipped_rows()
        flat = tomlkit.table()
        for key in sorted(rows):
            flat[key] = rows[key]
        assert tomllib.loads(_dumped(grouped_settings_table(rows))) == tomllib.loads(_dumped(flat))

    def test_the_shipped_file_parses_to_the_same_rows_a_flat_rendering_would(self) -> None:
        rows = _shipped_rows()
        flat = tomlkit.table()
        for key in sorted(rows):
            flat[key] = rows[key]
        assert rows == tomllib.loads(_dumped(flat))["teatree"]

    def test_no_group_banner_is_mistaken_for_a_key_assignment(self) -> None:
        assert all(" = " not in line for line in _banner_comments())
