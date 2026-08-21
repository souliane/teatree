"""Where each config key sits in the NESTED declaration hierarchy — the grouping tree.

Membership AND hierarchy are DERIVED, never re-listed here:

*   a key's group is the ``UserSettings`` declaration base that declares it, and that
    base's own ``GROUP_PATH`` class var is the nested path it renders under;
*   the set of bases and their ORDER come off ``UserSettings.__mro__`` — the bases tuple
    IS the render order, so adding a base adds a group with nothing to register;
*   a key no base declares is grouped by the registry that registers it, each registry
    declaring its own path beside its keys (``COLD_SETTINGS_GROUP_PATH`` and friends).

So this module names no category and lists no key: adding a setting to an existing
declaration places it in the right nested group with zero edits anywhere.
``tests/config/test_settings_group_partition.py`` pins the declaration bases pairwise-
disjoint and exhaustive, over the tree as well as the flat field set.

:data:`UNGROUPED_PATH` is the never-vanish guarantee, not a resting place: a key no
declaration owns — and a base that declares no path at all — still gets a bucket and
renders under a visible banner, rather than being dropped from the page the way the
retired band classifier dropped 130 of 184.

:func:`group_tree` is the single nesting mechanism the dashboard, the TOML export and
the ``config_setting`` CLI all render from, so the three surfaces cannot disagree about
the hierarchy.

Deliberately pydantic-free — it composes the same cold-safe registries
``teatree.config``'s package init already loads, so importing it costs no schema import.
"""

import dataclasses
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, cast

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS, COLD_HOOK_SETTINGS_GROUP_PATH
from teatree.config.registries import (
    COLD_SETTINGS,
    COLD_SETTINGS_GROUP_PATH,
    REGISTRY_SETTINGS,
    REGISTRY_SETTINGS_GROUP_PATH,
)
from teatree.config.setting_help import setting_help
from teatree.config.settings import UserSettings

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

#: The bucket a key no declaration owns lands in — rendered last, under a visible banner.
UNGROUPED_PATH: tuple[str, ...] = ("Ungrouped",)

#: The registry-owned keys, each paired with the path its own module declares. A
#: declaration base wins over a registry when both would claim a key.
_REGISTRY_SOURCES: tuple[tuple[tuple[str, ...], Mapping[str, object]], ...] = (
    (COLD_SETTINGS_GROUP_PATH, COLD_SETTINGS),
    (COLD_HOOK_SETTINGS_GROUP_PATH, COLD_HOOK_SETTINGS),
    (REGISTRY_SETTINGS_GROUP_PATH, REGISTRY_SETTINGS),
)


@dataclasses.dataclass(frozen=True, slots=True)
class SettingGroupNode[RowT]:
    """One node of the grouping tree — a named level holding either children or rows.

    A node carries rows only when it is a leaf, so a reader never sees a table sitting
    above that same group's subsections.
    """

    label: str
    path: tuple[str, ...]
    rows: tuple[RowT, ...]
    children: tuple["SettingGroupNode[RowT]", ...]

    @property
    def depth(self) -> int:
        """How many levels down this node sits — 1 for a top-level group."""
        return len(self.path)

    @property
    def is_ungrouped(self) -> bool:
        """Whether this is the leftovers bucket, which renders under a visible banner."""
        return self.path == UNGROUPED_PATH


def _declaration_bases() -> "tuple[type[DataclassInstance], ...]":
    """Every ``UserSettings`` declaration base, in the order the bases tuple declares.

    ``__mro__`` is typed as plain ``type``; every entry here is a dataclass declaration
    base by construction (``UserSettings`` is a dataclass and ``object`` is excluded),
    which is what ``dataclasses.fields`` requires of its argument.
    """
    bases = (base for base in UserSettings.__mro__ if base not in {UserSettings, object})
    return cast("tuple[type[DataclassInstance], ...]", tuple(bases))


def _declared_path(base: type) -> tuple[str, ...]:
    """*base*'s declared path — the leftovers bucket when it declares none."""
    return getattr(base, "GROUP_PATH", None) or UNGROUPED_PATH


def _path_by_key() -> dict[str, tuple[str, ...]]:
    paths = {key: path for path, source in _REGISTRY_SOURCES for key in source}
    for base in _declaration_bases():
        paths.update(dict.fromkeys((f.name for f in dataclasses.fields(base)), _declared_path(base)))
    return paths


_PATH_BY_KEY: dict[str, tuple[str, ...]] = _path_by_key()


def setting_group_path(key: str) -> tuple[str, ...]:
    """The nested path *key* renders under — :data:`UNGROUPED_PATH` when none owns it."""
    return _PATH_BY_KEY.get(key, UNGROUPED_PATH)


def group_slug(path: Sequence[str]) -> str:
    """A URL-safe id for a group path, derived from the labels themselves.

    The dashboard addresses one group per request, so the path has to survive a URL. It is
    derived rather than declared: a renamed group changes its own id and nothing else has
    to be kept in step. Callers resolve a slug back by matching it against the live leaf
    paths, so an unknown one is a miss rather than a wrong group.
    """
    return "/".join("-".join("".join(c if c.isalnum() else " " for c in level).split()).lower() for level in path)


def group_paths() -> tuple[tuple[str, ...], ...]:
    """Every declared leaf path in render order, with the leftovers bucket last.

    Declaration bases come first in MRO order, then the registries; a path is listed
    once, at its first appearance, so the order is a function of the declarations alone.
    """
    declared = [_declared_path(base) for base in _declaration_bases()]
    declared += [path for path, _ in _REGISTRY_SOURCES]
    ordered = dict.fromkeys(path for path in declared if path != UNGROUPED_PATH)
    return (*ordered, UNGROUPED_PATH)


def _order_index() -> dict[tuple[str, ...], int]:
    """Each declared path's render position, and each ANCESTOR's position with it.

    A parent sorts at its first child's position, so a subtree renders contiguously.
    """
    positions: dict[tuple[str, ...], int] = {}
    for index, path in enumerate(group_paths()):
        for depth in range(1, len(path) + 1):
            positions.setdefault(path[:depth], index)
    return positions


def _subtree[RowT](
    prefix: tuple[str, ...],
    grouped: Mapping[tuple[str, ...], Sequence[RowT]],
    order: Mapping[tuple[str, ...], int],
) -> tuple[SettingGroupNode[RowT], ...]:
    """The children of *prefix*, each a leaf holding rows or a parent holding children."""
    children: dict[tuple[str, ...], None] = dict.fromkeys(
        path[: len(prefix) + 1] for path in grouped if len(path) > len(prefix) and path[: len(prefix)] == prefix
    )
    nodes = []
    for child in sorted(children, key=lambda path: (order.get(path, len(order)), path)):
        rows = tuple(grouped.get(child, ()))
        nodes.append(
            SettingGroupNode(
                label=child[-1],
                path=child,
                rows=rows,
                children=_subtree(child, grouped, order),
            )
        )
    return tuple(nodes)


def group_tree[RowT](rows: Sequence[RowT], key_of: Callable[[RowT], str]) -> tuple[SettingGroupNode[RowT], ...]:
    """Partition *rows* into the nested group tree — total, so no row is ever dropped.

    Every row lands under :func:`setting_group_path` of its key, and that path is never
    empty, so the leaves partition *rows* exactly. A group no row reaches is omitted; the
    leftovers bucket is not, because its whole job is to be seen when it has members.
    """
    grouped: dict[tuple[str, ...], list[RowT]] = {}
    for row in rows:
        grouped.setdefault(setting_group_path(key_of(row)), []).append(row)
    return _subtree((), grouped, _order_index())


@dataclasses.dataclass(frozen=True, slots=True)
class GroupHeading:
    """A level of the hierarchy announced once, above the rows and levels beneath it."""

    depth: int
    label: str


@dataclasses.dataclass(frozen=True, slots=True)
class GroupSection[RowT]:
    """One leaf's rows, preceded by the levels first announced above them.

    ``depth`` is the leaf's own depth, so a text surface indents its rows one level
    deeper than the last heading without re-deriving the path.
    """

    depth: int
    headings: tuple[GroupHeading, ...]
    rows: tuple[RowT, ...]


def group_leaves[RowT](nodes: tuple[SettingGroupNode[RowT], ...]) -> tuple[SettingGroupNode[RowT], ...]:
    """*nodes* flattened to the row-carrying leaves, in render order."""
    return tuple(leaf for node in nodes for leaf in ([node] if not node.children else group_leaves(node.children)))


def group_outline[RowT](rows: Sequence[RowT], key_of: Callable[[RowT], str]) -> Iterator[GroupSection[RowT]]:
    """The tree as a linear stream of sections — each level announced exactly once.

    The shape a text surface needs: the TOML export renders a heading as an indented
    comment and the ``config_setting`` CLI as an indented line, so both express the same
    hierarchy from one walk rather than each re-deriving it.
    """
    announced: set[tuple[str, ...]] = set()
    for leaf in group_leaves(group_tree(rows, key_of)):
        fresh = tuple(leaf.path[:depth] for depth in range(1, len(leaf.path) + 1) if leaf.path[:depth] not in announced)
        announced.update(fresh)
        yield GroupSection(
            depth=len(leaf.path),
            headings=tuple(GroupHeading(depth=len(level), label=level[-1]) for level in fresh),
            rows=leaf.rows,
        )


def nested_value_table[ValueT](value: Mapping[str, ValueT]) -> tomlkit_items.Table:
    """A ``dict``-valued setting rendered as its own TOML table, recursively, key-sorted."""
    table = tomlkit.table()
    for key in sorted(value):
        inner = value[key]
        table[key] = nested_value_table(inner) if isinstance(inner, Mapping) else inner
    return table


def setting_comment(key: str) -> str:
    """What *key* ACCEPTS, then what it means — the two halves of its one-line comment.

    Without the first half a reader of the dump can see that ``wip`` is ``"full"`` but not
    whether it takes any string or one of four words, which is precisely the question the
    export exists to answer away from the dashboard. It is DERIVED from the schema
    (:func:`~teatree.config.setting_annotation.setting_annotation`), the same answer the
    dashboard's selects are built from, so the two surfaces cannot come to disagree.

    PUBLIC because a test asserting a rendered TOML line must compose its expectation from
    the same renderer the file is written by. Spelling the comment out a second time in a
    test is what let this join drift: the annotation half shipped while two export snapshots
    still expected the help sentence alone, and both surfaces claimed to be right.
    """
    # Deferred (PLC0415): `setting_annotation` reaches `schema`, whose ~110ms pydantic
    # import this module otherwise never pays for.
    from teatree.config.setting_annotation import setting_annotation  # noqa: PLC0415 — deferred: kept lazy

    return " — ".join(part for part in (setting_annotation(key), setting_help(key)) if part)


def _commented(key: str, value: object) -> tomlkit_items.Item:
    """*value* as a TOML item carrying *key*'s type, choices and help as a TRAILING comment.

    Trailing rather than a line above: a standalone ``#`` line inside ``[teatree]`` is what
    the retired comment-banner group headings looked like, and the shipped-file conformance
    suite still pins that no line there starts with one. On the key's own line the sentence
    reads as the annotation it is, and every parser of the block — including the one that
    splits ``key = value`` out of the file — sees the same key it always did.
    """
    item: tomlkit_items.Item = value if isinstance(value, tomlkit_items.Item) else tomlkit.item(value)
    comment = setting_comment(key)
    return item.comment(comment) if comment else item


def _group_subtable[ValueT](node: SettingGroupNode[str], rows: Mapping[str, ValueT]) -> tomlkit_items.Table:
    """One group level as a TOML table — its own keys, then its subsections.

    A level that holds only subsections is a super-table, so it prints no header of its
    own and its children read as one dotted path rather than a bare empty section.
    """
    table = tomlkit.table(is_super_table=not node.rows)
    for key in node.rows:
        table[key] = _commented(key, rows[key])
    for child in node.children:
        table[child.label] = _group_subtable(child, rows)
    return table


def grouped_settings_table[ValueT](rows: Mapping[str, ValueT]) -> tomlkit_items.Table:
    """A settings table whose keys read as the group tree's real NESTED sub-tables.

    The nesting is a FILE shape only: the flat key namespace stays the persisted contract
    every reader, env override, ``ConfigSetting`` row and cold sqlite3 read depends on, and
    :func:`~teatree.config.cold_defaults.flatten_settings_table` collapses the wrappers back
    on read. A group wrapper is decidable without a marker — it is a table whose name is not
    a declared setting.

    A ``dict``-valued SETTING (``speak`` / ``mr_reminder``) is a genuine nested setting, not
    a group, so it renders at the table's top level after the group wrappers rather than
    inside one. That keeps ``[teatree.speak]`` reachable at the path it has always had.

    The ONE renderer behind both TOML surfaces — the ``config_setting`` export dump and the
    shipped ``defaults.toml`` writer — so a snapshot can never flatten what the export
    groups. Ordering is the tree's, then key-sorted within a leaf: a function of the
    CONTENT and the declarations, never of insertion order.
    """
    scalars = {key: value for key, value in rows.items() if not isinstance(value, Mapping)}
    table = tomlkit.table()
    for node in group_tree(sorted(scalars), key_of=lambda key: key):
        table[node.label] = _group_subtable(node, scalars)
    for key in sorted(set(rows) - set(scalars)):
        table[key] = _commented(key, nested_value_table(cast("Mapping[str, ValueT]", rows[key])))
    return table


def grouped_key_order(keys: Sequence[str]) -> tuple[str, ...]:
    """*keys* in the order :func:`grouped_settings_table` emits them — the conformance oracle."""
    return tuple(key for section in group_outline(sorted(keys), key_of=lambda key: key) for key in section.rows)


__all__ = [
    "UNGROUPED_PATH",
    "GroupHeading",
    "GroupSection",
    "SettingGroupNode",
    "group_leaves",
    "group_outline",
    "group_paths",
    "group_slug",
    "group_tree",
    "grouped_key_order",
    "grouped_settings_table",
    "nested_value_table",
    "setting_comment",
    "setting_group_path",
]
