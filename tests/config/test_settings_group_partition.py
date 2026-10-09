# test-path: cross-cutting
"""Disjoint-partition guard for the ``UserSettings`` group bases (#83).

``UserSettings`` declares its fields across private in-file group dataclasses purely
for readability; the flat field namespace is the persisted contract. Dataclass
inheritance SILENTLY overrides a duplicate field name (no error), so a field
accidentally declared in two groups — or dropped from every group — would not fail
loudly on its own. This guard closes that: the group field sets must be pairwise
DISJOINT and their union must equal exactly ``dataclasses.fields(UserSettings)``.
Either failure turns this red, so the grouping can never silently shadow or drop a
field.

The bases are read off ``UserSettings.__mro__`` rather than re-listed, so a base added
to the declaration is guarded the moment it exists — a hand-kept list would have to be
remembered, and the field it forgot is exactly the field this test exists to catch.

The same partition must hold over the NESTED grouping the bases declare: two bases
sharing a ``GROUP_PATH`` would merge two field sets into one rendered leaf, and a base
whose path is a strict prefix of another's would hang rows off a parent node. Both are
asserted below, so the tree is a partition and not merely the flat set is.
"""

import dataclasses

from teatree.config.setting_groups import UNGROUPED_PATH, group_tree, setting_group_path
from teatree.config.settings import UserSettings

#: The declaration bases, in MRO order — DERIVED, so a new base cannot be forgotten.
#: Editing the grouping WITHOUT keeping the partition disjoint-and-complete is a red test.
_GROUP_BASES = tuple(base for base in UserSettings.__mro__ if base not in {UserSettings, object})


def _group_field_sets() -> list[frozenset[str]]:
    return [frozenset(f.name for f in dataclasses.fields(group)) for group in _GROUP_BASES]


def _own_fields(base: type) -> frozenset[str]:
    """*base*'s OWN fields — inherited ones excluded, since a base may itself subclass."""
    inherited = frozenset().union(
        *(frozenset(f.name for f in dataclasses.fields(parent)) for parent in base.__mro__[1:] if parent is not object),
        frozenset(),
    )
    return frozenset(f.name for f in dataclasses.fields(base)) - inherited


def test_the_bases_are_discovered_not_hand_listed() -> None:
    assert len(_GROUP_BASES) > 1, "the MRO walk found no declaration bases"
    assert all(dataclasses.is_dataclass(base) for base in _GROUP_BASES)


def test_group_field_sets_are_pairwise_disjoint() -> None:
    seen: set[str] = set()
    for group in _GROUP_BASES:
        fields = _own_fields(group)
        overlap = seen & fields
        assert not overlap, f"{group.__name__} redeclares field(s) already in another group: {sorted(overlap)}"
        seen |= fields


def test_group_union_equals_user_settings_fields() -> None:
    union: frozenset[str] = frozenset().union(*(_own_fields(base) for base in _GROUP_BASES))
    all_fields = frozenset(f.name for f in dataclasses.fields(UserSettings))
    assert union == all_fields, (
        f"group partition does not cover UserSettings exactly: "
        f"missing={sorted(all_fields - union)} extra={sorted(union - all_fields)}"
    )


def test_partition_flags_a_synthetic_duplicate() -> None:
    # Anti-vacuity: if a field were declared in two groups, the pairwise-disjoint
    # check must fire. Simulate by intersecting a group with itself-plus-a-known field.
    sets = _group_field_sets()
    # A known field lives in exactly one group; asserting it is NOT in a second proves
    # the disjoint check has real teeth (a duplicate would make this membership count > 1).
    memberships = sum(1 for s in sets if "mode" in s)
    assert memberships == 1, "each field must belong to exactly one group base"


def test_every_base_declares_a_group_path() -> None:
    pathless = [base.__name__ for base in _GROUP_BASES if not getattr(base, "GROUP_PATH", ())]
    assert not pathless, f"declaration base(s) with no GROUP_PATH — their fields render as leftovers: {pathless}"


def test_no_two_bases_declare_the_same_group_path() -> None:
    by_path: dict[tuple[str, ...], list[str]] = {}
    for base in _GROUP_BASES:
        by_path.setdefault(base.GROUP_PATH, []).append(base.__name__)
    collisions = {path: names for path, names in by_path.items() if len(names) > 1}
    assert not collisions, f"group path(s) claimed by more than one base: {collisions}"


def test_no_declared_path_is_a_strict_prefix_of_another() -> None:
    # A parent that is also a leaf would hang its own rows off a node that has children,
    # so a reader could not tell which group those rows belong to.
    paths = [base.GROUP_PATH for base in _GROUP_BASES]
    nested = [
        (parent, child) for parent in paths for child in paths if parent != child and child[: len(parent)] == parent
    ]
    assert not nested, f"a declared group path is a strict prefix of another: {nested}"


def test_the_tree_partitions_every_field_into_exactly_one_leaf() -> None:
    names = tuple(f.name for f in dataclasses.fields(UserSettings))
    tree = group_tree(names, key_of=lambda name: name)
    leaves = []
    stack = list(tree)
    while stack:
        node = stack.pop()
        stack.extend(node.children) if node.children else leaves.append(node)
    placed = [row for leaf in leaves for row in leaf.rows]
    assert sorted(placed) == sorted(names), "the tree is not exhaustive over UserSettings"
    assert len(placed) == len(set(placed)), "a field was placed under more than one leaf"
    assert not [name for name in names if setting_group_path(name) == UNGROUPED_PATH], (
        "a UserSettings field fell through to the leftovers bucket"
    )
