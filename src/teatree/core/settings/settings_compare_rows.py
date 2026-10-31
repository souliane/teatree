"""A comparable setting or seed row and each instance's reading of it."""

from dataclasses import dataclass, replace
from typing import Final

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.seed_defaults import SEED_ROW_FIELDS
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.core.setting_cell import (
    DriftVerdict,
    SettingCell,
    env_pin_refusal,
    settings_restore_url,
    settings_write_url,
    verdict_for,
)
from teatree.core.setting_control import SettingControl
from teatree.core.settings.seed_editing import write_refusal as seed_write_refusal
from teatree.core.settings.seed_editing import write_url as seed_write_url
from teatree.core.settings.settings_compare_rendering import (
    ABSENT_TEXT,
    KIND_RANK,
    OPAQUE_DIFFERS,
    OPAQUE_SAME,
    Reading,
    RowKind,
)
from teatree.core.settings.settings_compare_sync import SEED, SETTING, WITHHELD_CATEGORY, Disposition, Outcome, classify
from teatree.core.settings_snapshot import canonical_json
from teatree.core.settings_snapshot.serialisation import Json
from teatree.core.settings_snapshot.withholding import carries_stub

#: U+0000 cannot occur in a setting key, an overlay name or a captured value canonical JSON, so
#: it is the one safe sentinel: nothing real can canonicalise onto it. Written as an ESCAPE — a
#: raw NUL byte in the source would make git call the file binary.
ABSENT: Final = "\u0000absent"


#: The resolution tier that outranks every stored row: teatree reads env first.
ENV_SOURCE: Final = "env"


#: Limit identical rows in the all-rows view; differing rows remain fully visible.
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
    default_value: Json = None
    default_recorded: bool = False
    source: str = ""
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
    family: str = ""
    group: str = ""
    syncable: bool = True
    sync_note: str = ""
    redacted: bool = False
    category: str = ""
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
    def local_cell(self) -> Cell | None:
        return next((cell for cell in self.cells if cell.local), None)

    @property
    def import_source(self) -> Cell | None:
        """The cell an import into this box carries — the first OTHER instance holding an opinion."""
        return next((cell for cell in self.held if not cell.local), self.held[0] if self.held else None)

    @property
    def candidate(self) -> Json:
        source = self.import_source
        return source.value if source else None

    @property
    def equals_shipped_default(self) -> bool:
        """Whether the imported value equals this box's own shipped default, which the import skips."""
        source = self.import_source
        if source is None:
            return False
        target = self.local_cell or self.cells[0]
        return target.default_recorded and canonical_json(source.value) == canonical_json(target.default_value)

    @property
    def env_shadowed(self) -> bool:
        """Whether THIS box reads the key from a ``T3_*`` variable — a peer's pin shadows nothing here."""
        return self.source == ENV_SOURCE

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
            local = self.local_cell
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
    def local_edit(self) -> SettingCell | None:
        local = self.local_cell
        return self._edit_cell(local) if local else None

    @property
    def revert_url(self) -> str:
        edit = self.local_edit
        if edit is None:
            return ""
        local = self.local_cell
        if self.surface == SETTING and local is not None and not local.present:
            return settings_restore_url(self.title, self.scope)
        return edit.post_url

    def _apply_url(self, peer: Cell) -> str:
        if peer.local or not peer.present or not peer.known or not self.writable:
            return ""
        local = self.local_cell
        if local is None or not local.known or peer.opinion == local.opinion:
            return ""
        edit = self._edit_cell(local)
        if edit is None:
            return ""
        disposition = classify(replace(self, cells=(local, peer))).disposition
        if disposition is Disposition.CLEAR:
            return settings_restore_url(self.title, self.scope) if local.present else ""
        return edit.post_url if disposition is Disposition.IMPORT else ""

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
            default_text=(
                "not recorded"
                if not cell.default_recorded
                else "withheld"
                if carries_stub(cell.default_value)
                else canonical_json(cell.default_value)
            ),
            source=cell.source,
            wire_value=canonical_json(cell.value) if cell.present and not opaque else "",
            apply_url=self._apply_url(cell),
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
