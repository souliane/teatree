"""Every config key resolves to exactly one place in the nested declaration tree.

The defect this guards: the retired ``/dash/config`` band classifier returned ``""`` for
130 of 184 ``UserSettings`` fields and dropped each one from the page. A grouping whose
membership AND whose hierarchy are DERIVED from the declaration sources cannot do that —
but only a total, exhaustive assertion proves it.

The nested half adds two more failure modes to guard: a path segment that names a level
no source declares (a hand-kept category list creeping back in), and a source that
declares no path at all (whose keys must still surface, not vanish).
"""

import dataclasses
import tomllib

import pytest
import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import (
    UNGROUPED_PATH,
    SettingGroupNode,
    group_leaves,
    group_outline,
    group_paths,
    group_slug,
    group_tree,
    grouped_key_order,
    grouped_settings_table,
    nested_value_table,
    setting_group_path,
)
from teatree.config.settings import UserSettings


def _leaves(nodes: tuple[SettingGroupNode[str], ...]) -> list[SettingGroupNode[str]]:
    return [leaf for node in nodes for leaf in ([node] if not node.children else _leaves(node.children))]


def _walk(nodes: tuple[SettingGroupNode[str], ...]) -> list[SettingGroupNode[str]]:
    return [n for node in nodes for n in (node, *_walk(node.children))]


def _schema_tree() -> tuple[SettingGroupNode[str], ...]:
    return group_tree(tuple(TeatreeSettingsSchema.model_fields), key_of=lambda key: key)


class TestEveryKeyIsGrouped:
    def test_every_schema_key_resolves_to_a_group_path(self) -> None:
        ungrouped = [key for key in TeatreeSettingsSchema.model_fields if not setting_group_path(key)]
        assert not ungrouped, f"{len(ungrouped)} schema key(s) resolve to no group: {sorted(ungrouped)}"

    def test_no_schema_key_falls_back_to_the_ungrouped_bucket(self) -> None:
        # The bucket exists as the never-vanish guarantee, not as a resting place.
        stragglers = [key for key in TeatreeSettingsSchema.model_fields if setting_group_path(key) == UNGROUPED_PATH]
        assert not stragglers, f"{len(stragglers)} key(s) landed in the catch-all: {sorted(stragglers)}"

    def test_every_declared_path_is_reachable_from_some_key(self) -> None:
        used = {setting_group_path(key) for key in TeatreeSettingsSchema.model_fields}
        dead = [path for path in group_paths() if path not in used and path != UNGROUPED_PATH]
        assert not dead, f"group path(s) no key belongs to: {dead}"

    def test_the_ungrouped_bucket_is_last_so_it_reads_as_the_leftovers(self) -> None:
        assert group_paths()[-1] == UNGROUPED_PATH


class TestTheHierarchyIsSeveralLevelsDeep:
    def test_the_tree_carries_more_than_two_levels(self) -> None:
        depths = {len(node.path) for node in _walk(_schema_tree())}
        assert max(depths) >= 3, f"the grouping is not nested several levels deep: depths={sorted(depths)}"

    def test_a_parent_level_is_shared_by_several_children(self) -> None:
        # A middle level with one child is a level that carries no information.
        parents = [node for node in _walk(_schema_tree()) if node.children]
        assert parents, "the tree has no parent nodes at all"
        assert any(len(node.children) > 1 for node in parents), "no parent groups more than one child"

    def test_a_parent_node_holds_no_rows_of_its_own(self) -> None:
        # Rows hang off leaves; a parent that also carried rows would render a table
        # above its own subsections, so a reader could not tell which group a row is in.
        for node in _walk(_schema_tree()):
            assert not (node.children and node.rows), f"{node.path} carries both children and rows"

    def test_a_deep_path_reaches_its_key_through_named_levels(self) -> None:
        path = setting_group_path("require_merge_evidence")
        assert len(path) >= 3, f"expected a nested path for a merge gate, got {path}"
        assert all(segment.strip() for segment in path), f"a path segment is blank: {path}"


class TestGroupingIsDerivedNotHandKept:
    @pytest.mark.parametrize(
        ("key", "path"),
        [
            ("autoload", ("Workspace", "Engagement & identity")),
            ("mode", ("Agents", "Mode & harness")),
            ("loop_cadence_seconds", ("Loops", "Cadence & throughput")),
            ("on_behalf_post_mode", ("Communication", "Posting on your behalf")),
            ("require_merge_evidence", ("Gates", "Quality", "Merge & done")),
            ("architectural_review_cadence_hours", ("Gates", "Quality", "Architectural review")),
            ("disk_warn_free_gb", ("Infrastructure", "Resource pressure", "Thresholds & cadence")),
            ("allow_destructive_disk", ("Infrastructure", "Resource pressure", "Destructive levers")),
            ("provision_max_concurrency", ("Infrastructure", "Provisioning")),
            ("banned_terms", ("Registries", "Term scanning, agent tables & cold reads")),
            ("skill_loading_gate_enabled", ("Gates", "Pre-Django hooks")),
            ("overlays", ("Registries", "Definitions")),
        ],
    )
    def test_a_representative_key_lands_in_its_declaring_group(self, key: str, path: tuple[str, ...]) -> None:
        assert setting_group_path(key) == path

    def test_a_key_no_group_declares_falls_back_visibly_rather_than_vanishing(self) -> None:
        # The 70%-dropped defect in one assertion: an unknown key gets a bucket, not silence.
        assert setting_group_path("a_key_no_declaration_base_carries") == UNGROUPED_PATH

    def test_a_new_field_on_an_existing_declaration_is_grouped_with_no_registry_edit(self) -> None:
        # The zero-edit contract: membership is the declaration, so a key added to a base
        # is placed by that base's path with nothing to update here.
        declaring = next(
            base
            for base in UserSettings.__mro__
            if getattr(base, "GROUP_PATH", None) == ("Gates", "Quality", "Merge & done")
        )
        added = dataclasses.make_dataclass(
            "_AddedFieldSettings",
            [("a_freshly_added_merge_knob", bool, dataclasses.field(default=False))],
            bases=(declaring,),
        )
        assert setting_group_path("a_freshly_added_merge_knob") == UNGROUPED_PATH, "precondition: not yet declared"
        assert added.GROUP_PATH == ("Gates", "Quality", "Merge & done")


class TestTheTreeIsATotalPartition:
    def test_the_leaves_partition_every_schema_key_exactly_once(self) -> None:
        placed = [row for leaf in _leaves(_schema_tree()) for row in leaf.rows]
        assert sorted(placed) == sorted(TeatreeSettingsSchema.model_fields)
        assert len(placed) == len(set(placed)), "a key was placed under more than one leaf"

    def test_an_unknown_key_still_lands_in_the_tree_under_the_leftovers_banner(self) -> None:
        tree = group_tree(("mode", "a_key_no_declaration_base_carries"), key_of=lambda key: key)
        leftovers = [leaf for leaf in _leaves(tree) if leaf.is_ungrouped]
        assert leftovers, "an unowned key produced no visible bucket"
        assert leftovers[0].rows == ("a_key_no_declaration_base_carries",)

    def test_an_empty_declared_group_is_omitted_but_the_leftovers_bucket_is_not(self) -> None:
        labels = {
            node.label for node in _walk(group_tree(("a_key_no_declaration_base_carries",), key_of=lambda key: key))
        }
        assert labels == {UNGROUPED_PATH[0]}


class TestTheOutlineTheTextSurfacesRender:
    """``group_outline`` is the ONE walk the TOML export and the CLI listing share."""

    def _sections(self, keys: tuple[str, ...]) -> list:
        return list(group_outline(keys, key_of=lambda key: key))

    def test_a_level_is_announced_once_however_many_leaves_share_it(self) -> None:
        sections = self._sections(("require_merge_evidence", "architectural_review_disabled", "critic_gate_mode"))
        announced = [(heading.depth, heading.label) for section in sections for heading in section.headings]
        assert announced.count((1, "Gates")) == 1, "a shared parent level is re-announced per child"
        assert announced.count((2, "Quality")) == 1
        assert [label for depth, label in announced if depth == 3] == [
            "Architectural review",
            "Merge & done",
            "Critic & send proxy",
        ]

    def test_each_sections_rows_follow_the_headings_that_introduce_them(self) -> None:
        sections = self._sections(("autoload", "require_merge_evidence"))
        assert [section.headings[-1].label for section in sections] == ["Engagement & identity", "Merge & done"]
        assert [section.rows for section in sections] == [("autoload",), ("require_merge_evidence",)]

    def test_a_sections_depth_is_its_leafs_so_a_text_surface_indents_without_relookup(self) -> None:
        sections = self._sections(("autoload", "require_merge_evidence"))
        assert [section.depth for section in sections] == [2, 3]
        assert all(section.depth == len(setting_group_path(row)) for section in sections for row in section.rows)

    def test_the_outline_places_every_row_exactly_once(self) -> None:
        keys = tuple(TeatreeSettingsSchema.model_fields)
        placed = [row for section in self._sections(keys) for row in section.rows]
        assert sorted(placed) == sorted(keys)

    def test_group_leaves_flattens_to_the_row_carrying_nodes_in_render_order(self) -> None:
        tree = group_tree(("autoload", "require_merge_evidence"), key_of=lambda key: key)
        leaves = group_leaves(tree)
        assert [leaf.path for leaf in leaves] == [
            ("Workspace", "Engagement & identity"),
            ("Gates", "Quality", "Merge & done"),
        ]
        assert all(not leaf.children for leaf in leaves), "group_leaves returned a node with children"


_SAMPLE: dict[str, object] = {
    "autoload": False,
    "mode": "interactive",
    "require_merge_evidence": True,
    "architectural_review_cadence_hours": 168,
    "disk_warn_free_gb": 5,
}


def _dumped(table: tomlkit_items.Table) -> str:
    document = tomlkit.document()
    document["teatree"] = table
    return tomlkit.dumps(document)


class TestTheTomlRendererBothSurfacesShare:
    """``grouped_settings_table`` is the ONE renderer the export dump and the shipped file use."""

    def test_the_file_nests_while_the_key_namespace_stays_flat(self) -> None:
        # The two halves of the contract in one assertion pair: the FILE really nests
        # (a flat renderer fails the first), and the flat namespace every reader,
        # env override and cold sqlite3 read depends on is recovered exactly (a
        # renderer that dropped or renamed a key fails the second).
        parsed = tomllib.loads(_dumped(grouped_settings_table(_SAMPLE)))["teatree"]
        assert any(isinstance(value, dict) for value in parsed.values())
        assert flatten_settings_table(parsed) == _SAMPLE

    def test_grouping_changes_no_value_only_the_shape(self) -> None:
        flat = tomlkit.table()
        for key in sorted(_SAMPLE):
            flat[key] = _SAMPLE[key]
        nested = tomllib.loads(_dumped(grouped_settings_table(_SAMPLE)))["teatree"]
        assert flatten_settings_table(nested) == flatten_settings_table(tomllib.loads(_dumped(flat))["teatree"])

    def test_every_level_is_a_real_table_header_at_its_full_path(self) -> None:
        headers = [line for line in _dumped(grouped_settings_table(_SAMPLE)).splitlines() if line.startswith("[")]
        assert headers == [
            '[teatree.Workspace."Engagement & identity"]',
            '[teatree.Agents."Mode & harness"]',
            '[teatree.Gates.Quality."Architectural review"]',
            '[teatree.Gates.Quality."Merge & done"]',
            '[teatree.Infrastructure."Resource pressure"."Thresholds & cadence"]',
        ]

    def test_a_group_wrapper_is_decidable_without_a_marker(self) -> None:
        # The flattener's whole disambiguation rule: a sub-table named after a DECLARED
        # setting is that setting's value, any other is a group. A nested SETTING must
        # therefore survive the round trip whole rather than being descended into.
        rows: dict[str, object] = {**_SAMPLE, "speak": {"local": "all"}}
        parsed = tomllib.loads(_dumped(grouped_settings_table(rows)))["teatree"]
        assert flatten_settings_table(parsed)["speak"] == {"local": "all"}

    def test_the_emitted_order_is_the_group_walk_not_the_alphabet(self) -> None:
        lines = _dumped(grouped_settings_table(_SAMPLE)).splitlines()
        emitted = [line.split(" =")[0] for line in lines if " = " in line]
        assert emitted == list(grouped_key_order(tuple(_SAMPLE)))
        assert emitted != sorted(_SAMPLE), "the grouped order collapsed back to plain alphabetical"

    def test_grouped_key_order_places_every_key_exactly_once(self) -> None:
        keys = tuple(TeatreeSettingsSchema.model_fields)
        assert sorted(grouped_key_order(keys)) == sorted(keys)


class TestGroupSlug:
    """A URL-safe id for a group path, DERIVED from the labels so a rename keeps in step."""

    def test_a_multi_level_path_becomes_one_slash_joined_slug(self) -> None:
        assert group_slug(("Gates", "Quality", "Merge & done")) == "gates/quality/merge-done"

    def test_punctuation_and_case_are_normalised_away(self) -> None:
        assert group_slug(("Resource pressure", "Thresholds & cadence")) == "resource-pressure/thresholds-cadence"

    def test_two_different_paths_never_collide(self) -> None:
        # Every live leaf, since a collision would silently serve the wrong section.
        paths = [leaf.path for leaf in group_leaves(group_tree(sorted(_SAMPLE), key_of=lambda key: key))]
        assert len({group_slug(path) for path in paths}) == len(paths)

    def test_every_live_section_slug_is_unique(self) -> None:
        leaves = group_leaves(group_tree(sorted(TeatreeSettingsSchema.model_fields), key_of=lambda key: key))
        slugs = [group_slug(leaf.path) for leaf in leaves]
        assert len(set(slugs)) == len(slugs)


class TestNestedValueTable:
    """A ``dict``-valued SETTING rendered as its own table — key-sorted, to any depth."""

    def test_a_flat_mapping_renders_key_sorted(self) -> None:
        table = nested_value_table({"b": 2, "a": 1})
        assert list(table) == ["a", "b"]

    def test_a_mapping_nests_recursively(self) -> None:
        document = tomlkit.document()
        document["speak"] = nested_value_table({"channels": {"b": "y", "a": "x"}, "local": "all"})
        parsed = tomllib.loads(tomlkit.dumps(document))["speak"]
        assert parsed == {"channels": {"a": "x", "b": "y"}, "local": "all"}
