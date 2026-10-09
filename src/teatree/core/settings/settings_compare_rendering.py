"""How the comparison RENDERS: one reading per cell, and the groups the page leads with.

Every presentation decision the compare table makes is made here rather than in the template —
folding a value too long for a row, replacing a digest with the verdict it stands for, and which
kind of difference the page opens on. Kept apart from the diff because "what differs" and "how
it reads" are two concerns, and the template is meant to hold no judgement at all.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from teatree.core.setting_cell import SettingCell

if TYPE_CHECKING:
    from teatree.core.settings.settings_compare import CompareRow


#: The longest value a table row can hold and still be read across. A `loops` or `entities`
#: payload is a 400-character single-token object: rendered whole it is the row, and rendered
#: whole on eighty rows it is the page. Beyond this a value is offered FOLDED — one line the eye
#: can skim, the whole value one click away — because a value nobody can read is not a comparison.
FOLD_OVER: Final = 72

#: What a withheld value reads as. Two sha256 digests side by side carry no human signal at all:
#: the reader cannot tell which is which, cannot act on either, and the only fact the digests
#: actually establish — whether the two boxes hold the SAME secret — is the one the hex buries.
OPAQUE_SAME: Final = "same"
OPAQUE_DIFFERS: Final = "differs"

ABSENT_TEXT: Final = "— absent —"


class RowKind(StrEnum):
    VALUES = "values differ"
    OVERRIDE = "override on one box"
    CODE = "code-version"
    SAME = "no difference"


KIND_RANK: Final[dict[RowKind, int]] = {
    RowKind.VALUES: 0,
    RowKind.OVERRIDE: 1,
    RowKind.CODE: 2,
    RowKind.SAME: 3,
}


@dataclass(frozen=True, slots=True)
class Reading:
    """How ONE instance's answer RENDERS — folding, and the digest a withheld value stands for.

    Purely presentational: whether the cell may be EDITED, where its write goes and what a
    control holds are not decided here. They ride on *cell*, the same
    :class:`~teatree.core.setting_cell.SettingCell` the single-instance grid edits through, so
    the two pages cannot drift into two answers about how one key is written. A column offering
    no edit at all — every peer, and every row with no key to write — carries no cell.
    """

    label: str
    present: bool
    local: bool
    agrees: bool
    text: str
    opaque: bool = False
    cell: SettingCell | None = None

    @property
    def folded(self) -> bool:
        return len(self.text) > FOLD_OVER

    @property
    def brief(self) -> str:
        """The one line the row shows; :attr:`text` is what unfolding reveals."""
        return self.text if not self.folded else f"{self.text[: FOLD_OVER - 1]}\u2026"


#: What each group of rows MEANS, in the reader's words rather than the enum's.
GROUP_NOTES: Final[dict[RowKind, str]] = {
    RowKind.VALUES: "both boxes hold a value here and the values are not the same",
    RowKind.OVERRIDE: "one box has been given a value here and the other is on its default",
    RowKind.CODE: "a stored row for something that box's code does not declare — a leftover",
    RowKind.SAME: "no difference",
}


@dataclass(frozen=True, slots=True)
class RowGroup:
    """One kind of difference, so the page leads with the rows that are actually a conflict."""

    kind: RowKind
    rows: tuple["CompareRow", ...]

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def open(self) -> bool:
        """Only the conflicting group opens; the rest are one click away with their count shown."""
        return self.kind is RowKind.VALUES

    @property
    def note(self) -> str:
        return GROUP_NOTES[self.kind]


__all__ = [
    "ABSENT_TEXT",
    "FOLD_OVER",
    "GROUP_NOTES",
    "KIND_RANK",
    "OPAQUE_DIFFERS",
    "OPAQUE_SAME",
    "Reading",
    "RowGroup",
    "RowKind",
]
