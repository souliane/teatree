"""What a teatree import would DO with a differing row, and which rule decided it.

Kept apart from the diff because it answers a different question: the diff says what the boxes
disagree about, this says whether an import could carry the disagreement across. The rules name
the three ways an import fails QUIETLY — a value equal to the shipped default is skipped rather
than written, a field the interchange does not carry is rejected, and a key the target reads
from a ``T3_*`` variable is written and then ignored because env outranks every stored row. The
last is the worst, because it looks exactly like a sync that worked.
"""

import operator
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from teatree.core.settings.settings_compare import CompareRow

#: The registry category whose values never leave the box, redacted or not.
WITHHELD_CATEGORY: Final = "secret"

SETTING: Final = "setting"
SEED: Final = "seed"
_ANY_SURFACE: Final = "any"


class Disposition(StrEnum):
    IMPORT = "import"
    CLEAR = "clear"
    SHADOWED = "shadowed"
    MANUAL = "manual"
    BLOCKED = "blocked"


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
class Outcome:
    """What an import could do with this row, and which rule decided it."""

    disposition: Disposition
    rule: str
    reason: str


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


def classify(row: "CompareRow") -> Outcome:
    """*row*'s write state — the first rule of :data:`SYNC_RULES` whose fact holds."""
    for rule in _RULE_TABLE:
        if rule["surface"] not in {_ANY_SURFACE, row.surface}:
            continue
        if _FACTS[str(rule["id"])](row):
            return Outcome(rule["disposition"], str(rule["id"]), row.sync_note or str(rule["reason"]))
    message = f"no rule matched {row.title!r}: the table has lost its catch-all row"
    raise ValueError(message)


__all__ = [
    "SEED",
    "SETTING",
    "SYNC_RULES",
    "WITHHELD_CATEGORY",
    "Disposition",
    "Outcome",
    "classify",
]
