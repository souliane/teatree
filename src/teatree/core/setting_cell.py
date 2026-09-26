"""How one setting is EDITED in one COLUMN — the half a second grid shares with the first.

:class:`~teatree.core.setting_control.SettingControl` already answers "what is this key" —
help text, shipped default, masking verdict, admissible options — for every surface that
lists settings. This module answers the other half: given one key's resolution in ONE column,
what does that column's cell SAY about it, and can it be written. Deriving those twice is how
"how is this key edited" drifts into two answers, so both grids read them from here.

A column is not necessarily an overlay scope. A grid comparing BOXES has the same shape —
one key per row, one value per column — and its peer columns are read-only, which is a
:attr:`SettingCell.unwritable_reason` rather than a second kind of cell.
"""

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from django.urls import reverse

from teatree.config.provenance import ValueSource
from teatree.config.setting_registries import code_pin_refusal, env_pin
from teatree.core.setting_control import SettingControl, wire

#: The column header the global scope renders under — every other column is named by its scope.
GLOBAL_LABEL = "global"

#: Both env verdicts — a value the parser took, and one it refused. Either way the var pins.
_ENV_SOURCES: frozenset[str] = frozenset({ValueSource.ENV.value, ValueSource.INVALID_ENV.value})


class DriftVerdict(StrEnum):
    """What a cell can say about its value against the shipped default — THREE states, not two.

    ``NO_DEFAULT`` is the state the grid used to render as ``AT_DEFAULT``: a key the shipped
    file carries no entry for has nothing to equal, so claiming it matches a default asserts a
    comparison that was never made (#120).
    """

    AT_DEFAULT = "at-default"
    DRIFTED = "drifted"
    NO_DEFAULT = "no-default"


def env_pin_refusal(key: str, source: str) -> str:
    """Why a cell holding *key* cannot be written, or ``""`` — the one rule both grids ask.

    Two ways a stored write lands in a layer nothing reads back, reporting success and
    changing nothing an operator can observe: a ``T3_*`` var supplying the value, and a code
    path that reads the SHIPPED default for this key instead of the resolver. The second is
    the more dangerous of the two, because the cell then re-renders as drifted — the operator
    is shown their own write echoed back by a mechanism that ignores it.

    A var whose value the parser REFUSES still pins the key: the resolver raises on it, so a
    stored write is no more readable than under a valid pin.
    """
    if source in _ENV_SOURCES and (pinned := env_pin(key)):
        return f"pinned by {pinned}"
    return code_pin_refusal(key)


def verdict_for(control: SettingControl, *, overridden: bool, value: str) -> DriftVerdict:
    """One cell against the shipped default — compared as the operator SEES it.

    Identical text in the cell means identical value. An operator tier outranking a code
    default IS the drift a grid exists to surface; a key with no shipped entry to equal gets
    its own verdict rather than a claim about a comparison nothing performed.
    """
    if overridden:
        at_default = control.has_shipped_default and value == control.shipped_default
        return DriftVerdict.AT_DEFAULT if at_default else DriftVerdict.DRIFTED
    return DriftVerdict.AT_DEFAULT if control.has_shipped_default else DriftVerdict.NO_DEFAULT


@dataclass(frozen=True, slots=True)
class SettingCell:
    """One column's value for one setting — the unit a grid renders and an edit posts."""

    key: str
    scope: str  # "" is global
    value: str  # ``***`` for a secret, else the effective value as display text
    selected: str  # the JSON literal of the effective value — what a SELECT matches options on
    source: str  # which tier of the resolution chain supplied it — the cell's tooltip
    verdict: DriftVerdict
    unwritable_reason: str = ""  # empty means the cell is editable; else why it cannot be
    #: Where this cell's edit POSTs. Supplied by the builder rather than derived here: the
    #: two write surfaces address different things — a ``ConfigSetting`` row by key + scope,
    #: a seed row by table + entry + field — and putting the seed route's shape in this
    #: module would make it know about a surface it has no other business with.
    post_url: str = ""

    @property
    def label(self) -> str:
        return self.scope or GLOBAL_LABEL

    @property
    def drifts(self) -> bool:
        return self.verdict is DriftVerdict.DRIFTED

    @property
    def writable(self) -> bool:
        """Whether a write to this cell can actually take effect.

        Offering an editable control where it cannot invites a write into a layer nothing reads
        back — it reports success and changes nothing an operator can observe.
        """
        return not self.unwritable_reason

    @property
    def editable(self) -> str:
        """What a FREE-TEXT control shows — the JSON literal, except that unset renders empty.

        A control holds what it would POST, which is the right contract for a value the
        operator edits as JSON (a list, a table, a quoted string). It is the wrong thing to
        show for ``None``: the page put the four letters ``null`` in front of a human, who then
        had to know the wire encoding to read that the setting is simply not set (#4078).

        An empty box is the honest rendering of an unset value, and it is already the grid's
        own vocabulary — emptying a cell IS the restore-to-default gesture. :attr:`selected`
        keeps the literal, so a ``<select>`` still matches its ``null`` option.
        """
        return "" if self.selected == wire(None) else self.selected


def settings_write_url(key: str, scope: str) -> str:
    """Where a ``ConfigSetting`` cell's edit goes — the key in the path, its scope in the query.

    One derivation for every grid that edits a setting: a scope-qualified POST to
    ``settings/set/<key>/``. A cell that ignored the scope would write the wrong layer.
    """
    url = reverse("dash:settings_set", args=[key])
    return f"{url}?scope={quote(scope)}" if scope else url


__all__ = [
    "GLOBAL_LABEL",
    "DriftVerdict",
    "SettingCell",
    "env_pin_refusal",
    "settings_write_url",
    "verdict_for",
]
