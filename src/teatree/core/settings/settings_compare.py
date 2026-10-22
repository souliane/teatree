"""What actually differs between the instances, and what an import could do about each row.

**The no-opinion rule is the whole correctness of this page.** A box that holds no stored row
for a key at a scope has NO OPINION there, and every way of having none is the SAME fact, not
three different ones:

*   the box holds no row for this key in this scope,
*   the box holds no rows in this scope at all (a scope it simply does not use), or
*   the box's code does not declare the key, so it could never hold a row for it.

All three canonicalise to :data:`ABSENT` and therefore compare EQUAL to each other; only a
STORED value differs from them. Treating any of them as a distinct value is how a scope one
box does not use becomes one fabricated difference per key, and how a key one box's code lacks
gets repeated as a difference in every scope. Where the code disagrees about which keys exist
is stated ONCE, as a key-set difference, by :mod:`teatree.core.settings.settings_compat` — that is its
home, not this diff.

A stored row for a key the box's code does NOT declare stays visible and is called what it is
(:attr:`RowKind.CODE`, "code-version"): it is a leftover from other code, worth seeing, and
never a fabricated difference.

**A column belongs to the box above it.** The difference table's headings and its cells are
both read off :attr:`CompareView.compared` — the instances that ANSWERED — so a peer going down
can never slide a value under another box's name. The peers that did not answer are named in
full, with their reason, beside the table rather than inside it.

**A column is a live peer or a loaded record, and the rules do not fork.** A snapshot file the
operator loaded (:mod:`teatree.core.settings.settings_files`) enters :func:`build_compare_view` as one
more instance and is diffed by everything above unchanged — which is what lets an offline box,
a decommissioned box, and this box as it stood weeks ago be compared at all. The no-opinion
rule is why an older capture stays readable: whatever a newer teatree declares and an older one
never did is silence on the record's side, so it compares EQUAL instead of appearing as drift.

**Row write states** are :data:`SYNC_RULES`, matched in order, first match wins. They name the
three ways a teatree import fails QUIETLY — a value equal to the shipped default is skipped
rather than written, a field the interchange does not carry is rejected, and a key the target
reads from a ``T3_*`` variable is written and then ignored because env outranks every stored
row. The last is the worst, because it looks exactly like a sync that worked.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.declared_defaults import declared_defaults
from teatree.config.override_reader import column_spelling, fold_overlay_rows
from teatree.config.secret_settings import is_pass_key_setting
from teatree.config.seed_defaults import SEED_FAMILIES
from teatree.config.settings import OverlayEntry
from teatree.core.setting_control import SettingControl
from teatree.core.settings.settings_compare_rendering import (
    GROUP_NOTES,
    KIND_RANK,
    OPAQUE_DIFFERS,
    OPAQUE_SAME,
    Reading,
    RowGroup,
    RowKind,
)
from teatree.core.settings.settings_compare_rows import (
    ABSENT,
    CONFIRM_GATED_REASON,
    ENV_SOURCE,
    RENDER_CAP,
    WITHHELD_REASON,
    Cell,
    CompareRow,
)
from teatree.core.settings.settings_compare_sync import SEED, SETTING
from teatree.core.settings.settings_compat import CompatReport, build_compat_report
from teatree.core.settings.settings_peers import PeerSnapshot, local_snapshot, peer_snapshots
from teatree.core.settings_snapshot import canonical_json
from teatree.core.settings_snapshot.payload import _setting_family
from teatree.core.settings_snapshot.withholding import carries_stub


@dataclass(frozen=True, slots=True)
class CompareFilters:
    scope: str = "global"
    family: str = ""
    group: str = ""
    text: str = ""
    differs_only: bool = True


@dataclass(frozen=True, slots=True)
class CompareView:
    """The page: which instances answered, whether they may be compared, and what differs.

    *instances* is every CONFIGURED box plus every LOADED record, and *compared* is the subset
    that answered. They are two different facts and the page needs both — the instance band
    names every peer and why a missing one is missing, while the difference table can only have
    a column per instance that actually reported a value.
    """

    instances: tuple[PeerSnapshot, ...] = ()
    compared: tuple[PeerSnapshot, ...] = ()
    compat: CompatReport | None = None
    rows: tuple[CompareRow, ...] = ()
    total_rows: int = 0
    #: Every row the diff BUILT, differing or not, so the page can say how many it left out
    #: rather than leaving the reader to wonder whether a key is missing or merely agreed.
    compared_rows: int = 0
    #: Every differing row before a caller narrows the view by kind.
    differing_rows: int = 0
    error: str = ""
    families: tuple[str, ...] = ()
    setting_groups: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()

    @property
    def labels(self) -> tuple[str, ...]:
        """The difference table's column headings — read off the sequence the CELLS come from.

        Taking them from *instances* instead puts a heading above no cell: with a peer down in
        the middle, every remaining box's value renders one column left of its own name, so the
        table attributes each value to the wrong instance while looking entirely ordinary.
        """
        return tuple(instance.label for instance in self.compared)

    @property
    def unreachable(self) -> tuple[PeerSnapshot, ...]:
        return tuple(instance for instance in self.instances if not instance.reachable)

    @property
    def truncated(self) -> bool:
        return self.total_rows > len(self.rows)

    @property
    def identical(self) -> int:
        """How many rows the two boxes agreed on — the count that keeps nothing looking hidden."""
        return self.compared_rows - self.differing_rows

    @property
    def groups(self) -> tuple[RowGroup, ...]:
        """The shown rows partitioned by kind, conflicts first — a partition, never a filter."""
        buckets: dict[RowKind, list[CompareRow]] = {}
        for row in self.rows:
            buckets.setdefault(row.kind, []).append(row)
        ordered = sorted(buckets.items(), key=lambda item: KIND_RANK[item[0]])
        return tuple(RowGroup(kind=kind, rows=tuple(rows)) for kind, rows in ordered)


def build_compare_view(
    loaded: Sequence[PeerSnapshot] = (),
    *,
    timeout: float | None = None,
    kinds: Sequence[RowKind] = (),
    filters: CompareFilters | None = None,
) -> CompareView:
    """Fetch every peer, add every loaded record, and build the rows the instances differ on.

    *timeout* raises the per-peer wait for a caller that would rather wait than be told a slow
    forward did not answer; ``None`` leaves each peer on the wait its registry entry declares.
    """
    filters = filters or CompareFilters()
    instances = (local_snapshot(), *peer_snapshots(timeout), *loaded)
    live = tuple(instance for instance in instances if instance.reachable)
    compat = build_compat_report(instances)
    if len(live) < _MIN_INSTANCES:
        return CompareView(instances=instances, compat=compat, error=_NOTHING_TO_COMPARE)
    # Ordered by the SUBTITLE before the key, so every difference belonging to one scope — one
    # overlay, one loop, one preset — arrives as a contiguous block the page can set apart. Keyed
    # the other way round, a seed field shared by twenty loops produces twenty consecutive rows
    # with the same visible name and the distinguishing scope scattered down the column.
    all_rows = [*_setting_rows(live, "all"), *_seed_rows(live)]
    available_scopes = tuple(_comparison_scopes(live, "all"))
    available_families = tuple(sorted({row.family for row in all_rows if row.family}))
    available_groups = tuple(sorted({row.group for row in all_rows if row.group}))
    built = [row for row in all_rows if _matches_filters(row, filters)]
    differences = sorted((row for row in built if row.differs), key=reading_order)
    selected = differences if filters.differs_only else sorted(built, key=reading_order)
    if kinds:
        selected = [row for row in selected if row.kind in kinds]
    if filters.differs_only:
        shown = selected
    else:
        differing = [row for row in selected if row.differs]
        identical = [row for row in selected if not row.differs]
        shown = [*differing, *identical[:RENDER_CAP]]
    return CompareView(
        instances=instances,
        compared=live,
        compat=compat,
        rows=tuple(shown),
        total_rows=len(selected),
        compared_rows=len(built),
        differing_rows=len(differences),
        families=available_families,
        setting_groups=available_groups,
        scopes=available_scopes,
    )


def _matches_filters(row: CompareRow, filters: CompareFilters) -> bool:
    if row.surface == SETTING and filters.scope == "global" and row.scope:
        return False
    if (
        row.surface == SETTING
        and filters.scope not in {"global", "all"}
        and OverlayEntry.canonical_overlay_name(row.scope) != OverlayEntry.canonical_overlay_name(filters.scope)
    ):
        return False
    if filters.family and row.family != filters.family:
        return False
    if filters.group and filters.group.casefold() not in row.group.casefold():
        return False
    needle = filters.text.casefold().strip()
    return not needle or needle in f"{row.title} {row.subtitle} {row.group} {row.family}".casefold()


def reading_order(row: CompareRow) -> tuple[Any, ...]:
    """Conflicts first, then the rows the operator can actually DO something about.

    Ordering by surface name alone put every seed field ahead of every setting — so the whole
    first screen was rows the page cannot offer a control for, and the editable rows the owner
    came for started fifty rows down. Within a scope the subtitle groups a block, because keyed
    the other way a field shared by twenty loops gives twenty rows with the same visible name.
    """
    return (row.rank, not row.writable, row.surface, row.scope, row.subtitle, row.title)


_MIN_INSTANCES: Final = 2

_NOTHING_TO_COMPARE: Final = (
    "no reachable peer to compare against — bring a peer's tunnel up, or load a saved snapshot file below"
)


def _setting_rows(live: Sequence[PeerSnapshot], scope_filter: str) -> list[CompareRow]:
    keys = sorted(
        {
            key
            for instance in live
            for key in (
                *_registry(instance, "settings"),
                *(
                    key
                    for rows in _values(instance, "settings").values()
                    if isinstance(rows, Mapping)
                    for key in rows
                    if is_pass_key_setting(key)
                ),
            )
        }
    )
    scopes = _comparison_scopes(live, scope_filter)
    controls = _controls(keys)
    rows = []
    for scope in scopes:
        for key in keys:
            meta = _first_meta(live, key)
            rows.append(
                CompareRow(
                    surface=SETTING,
                    scope=scope,
                    title=key,
                    subtitle=f"overlay scope {scope}" if scope else "global scope",
                    cells=tuple(_setting_cell(instance, scope, key) for instance in live),
                    family=str(
                        meta.get("family")
                        or (
                            "credential"
                            if is_pass_key_setting(key)
                            else _setting_family(key)
                            if key in ALL_KNOWN_CONFIG_SETTINGS
                            else "setting"
                        )
                    ),
                    group=str(meta.get("group", "")),
                    syncable=meta.get("syncable", True) is not False,
                    sync_note=str(meta.get("sync_note", "")),
                    redacted=any(_is_redacted(instance, scope, key) for instance in live),
                    category=str(meta.get("category", "")),
                    equals_shipped_default=_equals_default(live, scope, key),
                    env_shadowed=any(_source(instance, scope, key) == ENV_SOURCE for instance in live),
                    source=_local_source(live, scope, key),
                    control=controls.get(key),
                )
            )
    return rows


def _setting_cell(instance: PeerSnapshot, scope: str, key: str) -> Cell:
    bucket = _setting_bucket(instance, scope)
    present = key in bucket
    defaults = _values(instance, "defaults")
    recorded = _default_recorded(instance, key)
    return Cell(
        label=instance.label,
        present=present,
        known=key in _registry(instance, "settings") or is_pass_key_setting(key),
        value=bucket.get(key) if present else None,
        default_value=defaults.get(key) if recorded else None,
        default_recorded=recorded,
        source=_source(instance, scope, key),
        local=instance.local,
    )


def _default_recorded(instance: PeerSnapshot, key: str) -> bool:
    if key not in _values(instance, "defaults"):
        return False
    version = (instance.payload or {}).get("format_version", 2)
    return version != 1 or key not in ALL_KNOWN_CONFIG_SETTINGS or _setting_family(key) != "table"


def _registered_scopes(instance: PeerSnapshot) -> set[str]:
    facts = (instance.payload or {}).get("instance", {})
    installed = facts.get("overlays_installed", ()) if isinstance(facts, Mapping) else ()
    registered = {str(row["name"]) for row in installed if isinstance(row, Mapping) and "name" in row}
    if isinstance(facts, Mapping) and facts.get("overlay"):
        registered.add(str(facts["overlay"]))
    return registered


def _scope_spelling(instance: PeerSnapshot, scope: str) -> str:
    if not scope:
        return ""
    canonical = OverlayEntry.canonical_overlay_name(scope)
    registered = {
        name for name in _registered_scopes(instance) if OverlayEntry.canonical_overlay_name(name) == canonical
    }
    if registered:
        return column_spelling(registered, registered)
    stored = {name for name in _values(instance, "settings") if OverlayEntry.canonical_overlay_name(name) == canonical}
    return column_spelling(stored, set()) if stored else scope


def _setting_bucket(instance: PeerSnapshot, scope: str) -> Mapping[str, Any]:
    stored = _values(instance, "settings")
    if not scope:
        return stored.get("", {})
    return fold_overlay_rows(stored, _scope_spelling(instance, scope)).rows


def _comparison_scopes(live: Sequence[PeerSnapshot], selected: str) -> list[str]:
    if selected == "global":
        return [""]
    all_scopes = {scope for instance in live for scope in _values(instance, "settings") if scope}
    groups: dict[str, set[str]] = {}
    for scope in all_scopes:
        groups.setdefault(OverlayEntry.canonical_overlay_name(scope), set()).add(scope)
    local = next((instance for instance in live if instance.local), None)
    local_registered = _registered_scopes(local) if local is not None else set()
    columns = sorted(column_spelling(group, local_registered) for group in groups.values())
    if selected == "all":
        return ["", *columns]
    canonical = OverlayEntry.canonical_overlay_name(selected)
    return [scope for scope in columns if OverlayEntry.canonical_overlay_name(scope) == canonical]


def _local_source(live: Sequence[PeerSnapshot], scope: str, key: str) -> str:
    """The tier THIS box resolved *key* from — a peer's tier says nothing about this box's edit."""
    return next((_source(instance, scope, key) for instance in live if instance.local), "")


def _controls(keys: Sequence[str]) -> dict[str, SettingControl]:
    """One control per key the schema knows, derived ONCE for the page rather than per row.

    The derivation is the same :class:`~teatree.core.setting_control.SettingControl` the
    single-instance grid edits through, so "how is this key edited" has one answer on both pages.
    A key no local schema declares — a peer on other code — gets none, and its row stays a
    reading.
    """
    shipped = declared_defaults()
    return {key: SettingControl(key, shipped) for key in keys if key in ALL_KNOWN_CONFIG_SETTINGS}


def _seed_rows(live: Sequence[PeerSnapshot]) -> list[CompareRow]:
    rows = []
    for table in sorted({table for instance in live for table in _registry(instance, "seed")}):
        fields = sorted({field for instance in live for field in _seed_fields(instance, table)})
        entities = sorted({name for instance in live for name in _values(instance, "seed").get(table, {})})
        for name in entities:
            for field in fields:
                meta = _seed_field_meta(live, table, field)
                rows.append(
                    CompareRow(
                        surface=SEED,
                        scope=table,
                        title=field,
                        subtitle=f"{table}.{name}",
                        entry=name,
                        cells=tuple(_seed_cell(instance, table, name, field) for instance in live),
                        family=next((name for name, key in SEED_FAMILIES.items() if key == table), table),
                        group=table,
                        syncable=meta.get("syncable", True) is not False,
                        sync_note=str(meta.get("sync_note", "")),
                        redacted=any(_seed_is_redacted(instance, table, name, field) for instance in live),
                        equals_shipped_default=_equals_seed_default(live, table, name, field),
                        seed_shipped=_seed_is_shipped(live, table, name),
                    )
                )
    return rows


def _seed_cell(instance: PeerSnapshot, table: str, name: str, field: str) -> Cell:
    entity = _values(instance, "seed").get(table, {}).get(name)
    present = isinstance(entity, Mapping) and field in entity
    shipped = _values(instance, "seed_shipped").get(table, {}).get(name)
    default_recorded = isinstance(shipped, Mapping) and field in shipped
    return Cell(
        label=instance.label,
        present=present,
        known=isinstance(entity, Mapping),
        value=entity.get(field) if present and isinstance(entity, Mapping) else None,
        default_value=shipped.get(field) if default_recorded and isinstance(shipped, Mapping) else None,
        default_recorded=default_recorded,
        source="stored" if present else "",
        local=instance.local,
    )


def _registry(instance: PeerSnapshot, surface: str) -> Mapping[str, Any]:
    value = instance.registry.get(surface)
    return value if isinstance(value, Mapping) else {}


def _values(instance: PeerSnapshot, surface: str) -> Mapping[str, Any]:
    value = instance.values.get(surface)
    return value if isinstance(value, Mapping) else {}


def _seed_fields(instance: PeerSnapshot, table: str) -> Mapping[str, Any]:
    entry = _registry(instance, "seed").get(table)
    fields = entry.get("fields") if isinstance(entry, Mapping) else None
    return fields if isinstance(fields, Mapping) else {}


def _first_meta(live: Sequence[PeerSnapshot], key: str) -> Mapping[str, Any]:
    for instance in live:
        meta = _registry(instance, "settings").get(key)
        if isinstance(meta, Mapping):
            return meta
    return {}


def _seed_field_meta(live: Sequence[PeerSnapshot], table: str, field: str) -> Mapping[str, Any]:
    for instance in live:
        meta = _seed_fields(instance, table).get(field)
        if isinstance(meta, Mapping):
            return meta
    return {}


def _is_redacted(instance: PeerSnapshot, scope: str, key: str) -> bool:
    """Whether the captured value carries a withhold stub — at ANY depth, not just its top.

    A registry row is withheld one FIELD at a time (an overlay definition's credential
    coordinate), so reading only the row's top level calls it an ordinary difference and the
    page offers to import a value that is a redaction stub.
    """
    return carries_stub(_setting_bucket(instance, scope).get(key))


def _seed_is_redacted(instance: PeerSnapshot, table: str, name: str, field: str) -> bool:
    entity = _values(instance, "seed").get(table, {}).get(name)
    return isinstance(entity, Mapping) and carries_stub(entity.get(field))


def _seed_is_shipped(live: Sequence[PeerSnapshot], table: str, name: str) -> bool:
    local = next((instance for instance in live if instance.local), None)
    return local is not None and isinstance(_values(local, "seed_shipped").get(table, {}).get(name), Mapping)


def _source(instance: PeerSnapshot, scope: str, key: str) -> str:
    """Which tier *instance* resolves *key* from in *scope*, falling back to its global reading.

    A scope the capture did not resolve answers with the global reading rather than nothing,
    because env is consulted before any scope at all; a more specific reading is never
    overruled by the global one.
    """
    all_scopes = _values(instance, "provenance")
    here = all_scopes.get(_scope_spelling(instance, scope))
    if isinstance(here, Mapping) and key in here:
        return str(here[key])
    globally = all_scopes.get("")
    return str(globally.get(key, "")) if isinstance(globally, Mapping) else ""


def _equals_default(live: Sequence[PeerSnapshot], scope: str, key: str) -> bool:
    stored = [_setting_bucket(instance, scope)[key] for instance in live if key in _setting_bucket(instance, scope)]
    if not stored:
        return False
    target = next((instance for instance in live if instance.local), live[0])
    defaults = _values(target, "defaults")
    return _default_recorded(target, key) and canonical_json(stored[0]) == canonical_json(defaults[key])


def _equals_seed_default(live: Sequence[PeerSnapshot], table: str, name: str, field: str) -> bool:
    shipped = _values(live[0], "seed_shipped").get(table, {}).get(name)
    if not isinstance(shipped, Mapping) or field not in shipped:
        return False
    entity = _values(live[0], "seed").get(table, {}).get(name)
    if not isinstance(entity, Mapping) or field not in entity:
        return False
    return canonical_json(entity[field]) == canonical_json(shipped[field])


__all__ = [
    "ABSENT",
    "CONFIRM_GATED_REASON",
    "ENV_SOURCE",
    "GROUP_NOTES",
    "OPAQUE_DIFFERS",
    "OPAQUE_SAME",
    "RENDER_CAP",
    "WITHHELD_REASON",
    "Cell",
    "CompareFilters",
    "CompareRow",
    "CompareView",
    "Reading",
    "RowGroup",
    "RowKind",
    "build_compare_view",
    "reading_order",
]
