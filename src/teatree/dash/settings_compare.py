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
is stated ONCE, as a key-set difference, by :mod:`teatree.dash.settings_compat` — that is its
home, not this diff.

A stored row for a key the box's code does NOT declare stays visible and is called what it is
(:attr:`RowKind.CODE`, "code-version"): it is a leftover from other code, worth seeing, and
never a fabricated difference.

**A column belongs to the box above it.** The difference table's headings and its cells are
both read off :attr:`CompareView.compared` — the instances that ANSWERED — so a peer going down
can never slide a value under another box's name. The peers that did not answer are named in
full, with their reason, beside the table rather than inside it.

**A column is a live peer or a loaded record, and the rules do not fork.** A snapshot file the
operator loaded (:mod:`teatree.dash.settings_files`) enters :func:`build_compare_view` as one
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

import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from teatree.core.settings_snapshot import canonical_json
from teatree.core.settings_snapshot.serialisation import Json
from teatree.core.settings_snapshot.withholding import carries_stub
from teatree.dash.settings_compat import CompatReport, build_compat_report
from teatree.dash.settings_peers import PeerSnapshot, local_snapshot, peer_snapshots

#: U+0000 cannot occur in a setting key, an overlay name or a captured value canonical JSON, so
#: it is the one safe sentinel: nothing real can canonicalise onto it. Written as an ESCAPE — a
#: raw NUL byte in the source would make git call the file binary.
ABSENT: Final = "\u0000absent"

SETTING: Final = "setting"
SEED: Final = "seed"
_ANY_SURFACE: Final = "any"

#: The resolution tier that outranks every stored row: teatree reads env first.
ENV_SOURCE: Final = "env"

#: The registry category whose values never leave the box, redacted or not.
WITHHELD_CATEGORY: Final = "secret"

#: The largest number of rows the page renders; a diff longer than this is a skew, not a diff.
RENDER_CAP: Final = 500


class Disposition(StrEnum):
    IMPORT = "import"
    CLEAR = "clear"
    SHADOWED = "shadowed"
    MANUAL = "manual"
    BLOCKED = "blocked"


class RowKind(StrEnum):
    VALUES = "values differ"
    OVERRIDE = "override on one box"
    CODE = "code-version"
    SAME = "no difference"


#: The disposition rule table: ordered, first match wins, each rule's id naming the FACT that
#: decides it. Held as data rather than as a chain of ifs so the whole rule document can be
#: read — and asserted — in one place.
SYNC_RULES: Final[tuple[dict[str, Any], ...]] = (
    {
        "order": 1,
        "id": "unrepresentable-value",
        "surface": _ANY_SURFACE,
        "disposition": Disposition.BLOCKED,
        "reason": "there is no TOML literal to emit",
    },
    {
        "order": 2,
        "id": "secret-withheld",
        "surface": _ANY_SURFACE,
        "disposition": Disposition.BLOCKED,
        "reason": "the value was withheld at capture and the import rejects secrets",
    },
    {
        "order": 3,
        "id": "no-import-path",
        "surface": _ANY_SURFACE,
        "disposition": Disposition.MANUAL,
        "reason": "the import rejects the field, so it has to be changed at the source",
    },
    {
        "order": 4,
        "id": "absent-on-target",
        "surface": SEED,
        "disposition": Disposition.MANUAL,
        "reason": "there is no row for the import to update",
    },
    {
        "order": 5,
        "id": "equals-default-setting",
        "surface": SETTING,
        "disposition": Disposition.CLEAR,
        "reason": "the import skips a value equal to the default, so the stored row must be cleared instead",
    },
    {
        "order": 6,
        "id": "equals-default-seed",
        "surface": SEED,
        "disposition": Disposition.MANUAL,
        "reason": "the import skips a value equal to the default and there is no clear command for a seed field",
    },
    {
        "order": 7,
        "id": "env-shadowed",
        "surface": SETTING,
        "disposition": Disposition.SHADOWED,
        "reason": "env outranks every stored row, so the import would write it and change nothing",
    },
    {
        "order": 8,
        "id": "differs",
        "surface": _ANY_SURFACE,
        "disposition": Disposition.IMPORT,
        "reason": "carried by the import TOML",
    },
)

_RULE_TABLE: Final = tuple(sorted(SYNC_RULES, key=operator.itemgetter("order")))


@dataclass(frozen=True, slots=True)
class Cell:
    """One instance's answer for one row — and, crucially, whether it has an OPINION at all."""

    label: str
    present: bool
    known: bool
    value: Json = None

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
        return "— absent —" if not self.present else canonical_json(self.value)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What an import could do with this row, and which rule decided it."""

    disposition: Disposition
    rule: str
    reason: str


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
        return _KIND_RANK[self.kind]

    @property
    def candidate(self) -> Json:
        """The value an import would carry — the first instance that holds an opinion at all."""
        return self.held[0].value if self.held else None

    @property
    def outcome(self) -> Outcome:
        return classify(self)


_KIND_RANK: Final[dict[RowKind, int]] = {
    RowKind.VALUES: 0,
    RowKind.OVERRIDE: 1,
    RowKind.CODE: 2,
    RowKind.SAME: 3,
}

_FACTS: Final[dict[str, Callable[["CompareRow"], bool]]] = {
    "unrepresentable-value": lambda row: row.candidate is None,
    "secret-withheld": lambda row: row.redacted or row.category == WITHHELD_CATEGORY,
    "no-import-path": lambda row: not row.syncable,
    "absent-on-target": lambda row: bool(row.blind),
    "equals-default-setting": lambda row: row.equals_shipped_default,
    "equals-default-seed": lambda row: row.equals_shipped_default,
    "env-shadowed": lambda row: row.env_shadowed,
    "differs": lambda _row: True,
}


def classify(row: CompareRow) -> Outcome:
    """*row*'s write state — the first rule of :data:`SYNC_RULES` whose fact holds."""
    for rule in _RULE_TABLE:
        if rule["surface"] not in {_ANY_SURFACE, row.surface}:
            continue
        if _FACTS[str(rule["id"])](row):
            return Outcome(rule["disposition"], str(rule["id"]), row.sync_note or str(rule["reason"]))
    message = f"no rule matched {row.title!r}: the table has lost its catch-all row"
    raise ValueError(message)


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


def build_compare_view(loaded: Sequence[PeerSnapshot] = ()) -> CompareView:
    """Fetch every peer, add every loaded record, and build the rows the instances differ on."""
    instances = (local_snapshot(), *peer_snapshots(), *loaded)
    live = tuple(instance for instance in instances if instance.reachable)
    compat = build_compat_report(instances)
    if len(live) < _MIN_INSTANCES:
        return CompareView(instances=instances, compat=compat, error=_NOTHING_TO_COMPARE)
    rows = sorted(_differing_rows(live), key=lambda row: (row.rank, row.surface, row.scope, row.title))
    return CompareView(
        instances=instances,
        compared=live,
        compat=compat,
        rows=tuple(rows[:RENDER_CAP]),
        total_rows=len(rows),
    )


_MIN_INSTANCES: Final = 2

_NOTHING_TO_COMPARE: Final = (
    "no reachable peer to compare against — bring a peer's tunnel up, or load a saved snapshot file below"
)


def _differing_rows(live: Sequence[PeerSnapshot]) -> list[CompareRow]:
    rows = [*_setting_rows(live), *_seed_rows(live)]
    return [row for row in rows if row.differs]


def _setting_rows(live: Sequence[PeerSnapshot]) -> list[CompareRow]:
    keys = sorted({key for instance in live for key in _registry(instance, "settings")})
    scopes = sorted({"", *(scope for instance in live for scope in _values(instance, "settings"))})
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
    )


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
                        cells=tuple(_seed_cell(instance, table, name, field) for instance in live),
                        syncable=meta.get("syncable", True) is not False,
                        sync_note=str(meta.get("sync_note", "")),
                        redacted=any(_seed_is_redacted(instance, table, name, field) for instance in live),
                        equals_shipped_default=_equals_seed_default(live, table, name, field),
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
    "ENV_SOURCE",
    "RENDER_CAP",
    "SEED",
    "SETTING",
    "SYNC_RULES",
    "Cell",
    "CompareRow",
    "CompareView",
    "Disposition",
    "Outcome",
    "RowKind",
    "build_compare_view",
    "classify",
]
