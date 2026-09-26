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
from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.seed_defaults import SEED_ROW_FIELDS
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.core.setting_cell import DriftVerdict, SettingCell, env_pin_refusal, settings_write_url, verdict_for
from teatree.core.setting_control import SettingControl
from teatree.core.settings.seed_editing import write_refusal as seed_write_refusal
from teatree.core.settings.seed_editing import write_url as seed_write_url
from teatree.core.settings.settings_compare_rendering import (
    ABSENT_TEXT,
    GROUP_NOTES,
    KIND_RANK,
    OPAQUE_DIFFERS,
    OPAQUE_SAME,
    Reading,
    RowGroup,
    RowKind,
)
from teatree.core.settings.settings_compare_sync import SEED, SETTING, WITHHELD_CATEGORY, Outcome, classify
from teatree.core.settings.settings_compat import CompatReport, build_compat_report
from teatree.core.settings.settings_peers import PeerSnapshot, local_snapshot, peer_snapshots
from teatree.core.settings_snapshot import canonical_json
from teatree.core.settings_snapshot.serialisation import Json
from teatree.core.settings_snapshot.withholding import carries_stub

#: U+0000 cannot occur in a setting key, an overlay name or a captured value canonical JSON, so
#: it is the one safe sentinel: nothing real can canonicalise onto it. Written as an ESCAPE — a
#: raw NUL byte in the source would make git call the file binary.
ABSENT: Final = "\u0000absent"


#: The resolution tier that outranks every stored row: teatree reads env first.
ENV_SOURCE: Final = "env"


#: The largest number of rows the page renders; a diff longer than this is a skew, not a diff.
RENDER_CAP: Final = 500

WITHHELD_REASON: Final = "the value was withheld at capture"

CONFIRM_GATED_REASON: Final = "confirm-gated — change it on the settings page"


@dataclass(frozen=True, slots=True)
class Cell:
    """One instance's answer for one row — and, crucially, whether it has an OPINION at all."""

    label: str
    present: bool
    known: bool
    value: Json = None
    #: Whether this answer is THIS process's own — the one column the page may write to.
    local: bool = False

    @property
    def opinion(self) -> str:
        """The canonical form this cell compares by — :data:`ABSENT` when it has no opinion."""
        if not self.present or not self.known:
            return ABSENT
        return canonical_json(self.value)

    @property
    def stale(self) -> bool:
        """A stored row for something this box's code does not declare — visible, never a diff."""
        return self.present and not self.known

    @property
    def text(self) -> str:
        return ABSENT_TEXT if not self.present else canonical_json(self.value)


@dataclass(frozen=True, slots=True)
class CompareRow:
    """One (surface, scope, key) across every instance."""

    surface: str
    scope: str
    title: str
    subtitle: str
    cells: tuple[Cell, ...]
    syncable: bool = True
    sync_note: str = ""
    redacted: bool = False
    category: str = ""
    equals_shipped_default: bool = False
    env_shadowed: bool = False
    #: Which tier THIS box resolved the key from — the cell's tooltip, and what the env-pin
    #: refusal is keyed on. Empty for a seed row and for a key this box's code does not declare.
    source: str = ""
    #: The seed entry addressed by this row. Empty on a ConfigSetting row.
    entry: str = ""
    #: Whether defaults.toml carries the seed entry, which is required for a restorative write.
    seed_shipped: bool = False
    #: The ONE derivation of how this key is edited, shared with the single-instance grid. Absent
    #: for a seed field and for a key this box's schema does not know — neither has a control.
    control: SettingControl | None = None

    @property
    def opinions(self) -> tuple[str, ...]:
        return tuple(cell.opinion for cell in self.cells)

    @property
    def held(self) -> tuple[Cell, ...]:
        return tuple(cell for cell in self.cells if cell.opinion != ABSENT)

    @property
    def blind(self) -> tuple[str, ...]:
        """The instances whose code cannot carry this row at all."""
        return tuple(cell.label for cell in self.cells if not cell.known)

    @property
    def stale_on(self) -> tuple[str, ...]:
        return tuple(cell.label for cell in self.cells if cell.stale)

    @property
    def opinions_differ(self) -> bool:
        """Whether the instances hold different OPINIONS — never merely different silences."""
        return len(set(self.opinions)) > 1

    @property
    def differs(self) -> bool:
        return self.opinions_differ or bool(self.stale_on)

    @property
    def kind(self) -> RowKind:
        if not self.opinions_differ:
            # A stored row on a box whose code has no such key is not an opinion, but it IS
            # worth seeing — shown, called what it is, and sorted with the code-version rows.
            return RowKind.CODE if self.stale_on else RowKind.SAME
        if len({cell.opinion for cell in self.held}) > 1:
            return RowKind.VALUES
        silent = len(self.cells) - len(self.held)
        if silent and silent == len(self.blind):
            return RowKind.CODE
        return RowKind.OVERRIDE

    @property
    def rank(self) -> int:
        return KIND_RANK[self.kind]

    @property
    def candidate(self) -> Json:
        """The value an import would carry — the first instance that holds an opinion at all."""
        return self.held[0].value if self.held else None

    @property
    def outcome(self) -> Outcome:
        return classify(self)

    @property
    def has_write_target(self) -> bool:
        """Whether a write from this page has anywhere to land — the two surfaces, both asked.

        Decided from the DECLARATIONS, never from whether a control happened to be attached:
        the control supplies a setting widget's options, and a row that lost one would
        otherwise go silently read-only rather than fail. A seed row's declaration is
        ``SEED_ROW_FIELDS`` — the same table the interchange writes through.
        """
        if self.surface == SEED:
            return self.title in SEED_ROW_FIELDS.get(self.scope, {})
        return self.surface == SETTING and self.title in ALL_KNOWN_CONFIG_SETTINGS

    @property
    def unwritable_reason(self) -> str:
        """Why this box's cell cannot be written, or ``""`` — one sentence the page SHOWS.

        A withheld value cannot be edited by a page that never showed it, and a safety-posture
        key's write is gated on a typed confirm phrase this page does not ask for — a second
        edit surface that did not ask for one would weaken that gate. The env tier is asked LAST
        and through the shared rule, so a key a ``T3_*`` var pins reads the same refusal here as
        it does on the settings grid.
        """
        if self.redacted or self.category == WITHHELD_CATEGORY:
            return WITHHELD_REASON
        if self.surface == SEED:
            # A seed field has no env tier and no confirm gate; what it can have is a
            # dedicated editor holding an invariant a raw write would skip.
            local = next((cell for cell in self.cells if cell.local), None)
            return seed_write_refusal(
                self.scope,
                self.entry,
                self.title,
                local_present=local is not None and local.known,
                shipped_entry=self.seed_shipped,
            )
        if self.title in SAFETY_POSTURE_KEYS:
            return CONFIRM_GATED_REASON
        return env_pin_refusal(self.title, self.source)

    @property
    def writable(self) -> bool:
        """Whether the operator can actually change this row HERE — what the ordering leads on."""
        return self.has_write_target and not self.unwritable_reason

    @property
    def readings(self) -> tuple[Reading, ...]:
        """One rendering per instance, decided against the row's own reference reading."""
        reference = self.held[0].opinion if self.held else ABSENT
        return tuple(self._reading(cell, reference) for cell in self.cells)

    def _reading(self, cell: Cell, reference: str) -> Reading:
        agrees = cell.opinion == reference
        opaque = self.redacted and cell.present
        return Reading(
            label=cell.label,
            present=cell.present,
            local=cell.local,
            agrees=agrees,
            text=(OPAQUE_SAME if agrees else OPAQUE_DIFFERS) if opaque else cell.text,
            opaque=opaque,
            cell=self._edit_cell(cell),
        )

    def _edit_cell(self, cell: Cell) -> SettingCell | None:
        """This box's answer as the EDIT the grid would make — ``None`` where there is no edit.

        No page may write over another box's tunnel, and a row nothing on this box declares has
        nowhere to write, so both carry no cell at all. Everything else about the edit — what a
        control holds, whether it may be offered — is
        :class:`~teatree.core.setting_cell.SettingCell`'s to answer, not this module's.
        """
        if not cell.local or not self.has_write_target:
            return None
        if self.surface == SEED:
            return self._seed_edit_cell(cell)
        if self.control is None:
            return None
        value = self.control.display_value(cell.value if cell.present else None)
        return SettingCell(
            key=self.title,
            scope=self.scope,
            value=value,
            selected=self.control.wire_value(cell.value if cell.present else None),
            source=self.source,
            verdict=verdict_for(self.control, overridden=cell.present, value=value),
            unwritable_reason=self.unwritable_reason,
            post_url=settings_write_url(self.title, self.scope),
        )

    def _seed_edit_cell(self, cell: Cell) -> SettingCell:
        """A loop / preset / schedule field as the edit this page would make.

        No shipped-default comparison: what a seed row is measured against lives in the
        ``defaults.toml`` seed table rather than in the settings defaults, and the page already
        shows that column. The verdict is therefore the honest ``NO_DEFAULT``, not a claim
        about a comparison this cell never performed.
        """
        return SettingCell(
            key=self.title,
            scope=self.scope,
            value=canonical_json(cell.value) if cell.present else ABSENT_TEXT,
            selected=canonical_json(cell.value) if cell.present else "",
            source="",
            verdict=DriftVerdict.NO_DEFAULT,
            unwritable_reason=self.unwritable_reason,
            post_url=seed_write_url(self.scope, self.entry, self.title),
        )


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
) -> CompareView:
    """Fetch every peer, add every loaded record, and build the rows the instances differ on.

    *timeout* raises the per-peer wait for a caller that would rather wait than be told a slow
    forward did not answer; ``None`` leaves each peer on the wait its registry entry declares.
    """
    instances = (local_snapshot(), *peer_snapshots(timeout), *loaded)
    live = tuple(instance for instance in instances if instance.reachable)
    compat = build_compat_report(instances)
    if len(live) < _MIN_INSTANCES:
        return CompareView(instances=instances, compat=compat, error=_NOTHING_TO_COMPARE)
    # Ordered by the SUBTITLE before the key, so every difference belonging to one scope — one
    # overlay, one loop, one preset — arrives as a contiguous block the page can set apart. Keyed
    # the other way round, a seed field shared by twenty loops produces twenty consecutive rows
    # with the same visible name and the distinguishing scope scattered down the column.
    built = [*_setting_rows(live), *_seed_rows(live)]
    differences = sorted((row for row in built if row.differs), key=reading_order)
    rows = [row for row in differences if row.kind in kinds] if kinds else differences
    return CompareView(
        instances=instances,
        compared=live,
        compat=compat,
        rows=tuple(rows[:RENDER_CAP]),
        total_rows=len(rows),
        compared_rows=len(built),
        differing_rows=len(differences),
    )


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


def _setting_rows(live: Sequence[PeerSnapshot]) -> list[CompareRow]:
    keys = sorted({key for instance in live for key in _registry(instance, "settings")})
    scopes = sorted({"", *(scope for instance in live for scope in _values(instance, "settings"))})
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
    bucket = _values(instance, "settings").get(scope, {})
    present = key in bucket
    return Cell(
        label=instance.label,
        present=present,
        known=key in _registry(instance, "settings"),
        value=bucket.get(key) if present else None,
        local=instance.local,
    )


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
    shipped = shipped_defaults_table()
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
    return Cell(
        label=instance.label,
        present=present,
        known=isinstance(entity, Mapping),
        value=entity.get(field) if present and isinstance(entity, Mapping) else None,
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
    return carries_stub(_values(instance, "settings").get(scope, {}).get(key))


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
    here = all_scopes.get(scope)
    if isinstance(here, Mapping) and key in here:
        return str(here[key])
    globally = all_scopes.get("")
    return str(globally.get(key, "")) if isinstance(globally, Mapping) else ""


def _equals_default(live: Sequence[PeerSnapshot], scope: str, key: str) -> bool:
    stored = [
        _values(instance, "settings").get(scope, {})[key]
        for instance in live
        if key in _values(instance, "settings").get(scope, {})
    ]
    if not stored:
        return False
    defaults = _values(live[0], "defaults")
    return key in defaults and canonical_json(stored[0]) == canonical_json(defaults[key])


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
    "CompareRow",
    "CompareView",
    "Reading",
    "RowGroup",
    "RowKind",
    "build_compare_view",
    "reading_order",
]
