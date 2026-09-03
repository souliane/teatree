"""Model-driven, secret-safe settings GRID for the dashboard (D7).

Walks the pydantic schema (``TeatreeSettingsSchema``) so every config key is listable
and editable with NO hand-kept list — a newly-added setting appears here for free. The
edit path writes through ``ConfigSetting.set_value`` (the same seam ``config_setting set``
uses), so the #258 strict coercion and the #3688 cross-key checks fire identically.

**One row per setting, every scope on it.** A row carries one :class:`ScopeCell` per scope —
global first, then each overlay — so "what is this set to everywhere" is one line rather
than a per-scope hunt. Each cell says whether it still equals the shipped default; a cell
that does not is the drift the page exists to make visible. A setting drifts at most ONCE
however many scopes override it, which is what the nav's per-section count answers: how
many settings here has someone changed, not how many override rows exist.

**One section per request.** The page is a left nav of sections and a right pane; selecting
a section ``hx-get``s that section alone. :func:`build_settings_sections` is the nav and
:func:`build_settings_group` the pane, both off the SAME tree, so a section the nav offers
always has a pane and the union of the panes is the whole schema.

**Every scope is read once for the whole page.** :class:`_Grid` resolves each scope's tiers
in a single :func:`~teatree.config.provenance.resolve_settings` call and every row reads out
of that, so a page of N scopes costs N settings reads rather than one per rendered cell.

**A secret value never reaches the response.** :func:`~teatree.core.config_display.is_secret`
(the shared value-masking taxonomy) drives masking here AND on the read-only config surface,
so the two pages apply ONE policy. A secret row's value AND its shipped default are replaced
with ``***`` HERE, before the row enters the view context. Transferring the whole store is a
page of its own (:mod:`teatree.dash.interchange`) — its scope is wider than this grid's.

**The per-setting half is composed, not restated.** Each row holds a
:class:`~teatree.core.setting_control.SettingControl` — the ONE derivation of a key's help
text, shipped default, masking verdict and admissible options, shared with the settings
snapshot. Help text is the same sentence ``defaults.toml`` carries as that key's comment and
the options are the schema's own admissible set, so a select can never offer a value the
validator refuses, and no second surface can drift into a different answer.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from django.urls import reverse

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.provenance import ResolvedSetting, ValueSource, resolve_settings
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import SettingGroupNode, group_leaves, group_slug, group_tree
from teatree.core.config_display import MASKED
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigValue
from teatree.core.overlay_loader import get_all_overlays
from teatree.core.setting_control import SettingChoice, SettingControl, wire

logger = logging.getLogger(__name__)

#: The column header the global scope renders under — every other column is an overlay name.
GLOBAL_LABEL = "global"


@dataclass(frozen=True, slots=True)
class ScopeCell:
    """One scope's value for one setting — the editable unit of the grid."""

    key: str
    scope: str  # "" is global
    value: str  # ``***`` for a secret, else the effective value as display text
    selected: str  # the JSON literal of the effective value — what a SELECT matches its options on
    source: str  # which tier of the resolution chain supplied it — the cell's tooltip
    matches_default: bool  # green when true, brown when the operator has changed it

    @property
    def label(self) -> str:
        return self.scope or GLOBAL_LABEL

    @property
    def editable(self) -> str:
        """What a FREE-TEXT control shows — the JSON literal, except that unset renders empty.

        A control holds what it would POST, which is the right contract for a value the
        operator edits as JSON (a list, a table, a quoted string). It is the wrong thing to
        show for ``None``: the page put the four letters ``null`` in front of a human, who
        then had to know the wire encoding to read that the setting is simply not set (#4078).

        An empty box is the honest rendering of an unset value, and it is already the page's
        own vocabulary — emptying a cell IS the restore-to-default gesture, so an untouched
        empty box that is never submitted changes nothing, and one that IS submitted clears a
        row that was already resolving its default. :attr:`selected` keeps the literal, so a
        ``<select>`` still matches its ``null`` option against the real value.
        """
        return "" if self.selected == wire(None) else self.selected

    @property
    def post_url(self) -> str:
        """Where this cell's edit goes — the key in the path, its own scope in the query."""
        url = reverse("dash:settings_set", args=[self.key])
        return f"{url}?scope={quote(self.scope)}" if self.scope else url


@dataclass(frozen=True, slots=True)
class EditableSetting:
    """One ROW of the grid — the setting's control, and every scope's value for it.

    The per-setting half is COMPOSED, not restated: the control is the same
    :class:`~teatree.core.setting_control.SettingControl` the snapshot surface derives from,
    delegated field by field so the row template and its readers keep one vocabulary.
    """

    control: SettingControl
    cells: tuple[ScopeCell, ...]

    @property
    def name(self) -> str:
        return self.control.name

    @property
    def help_text(self) -> str:
        return self.control.help_text

    @property
    def shipped_default(self) -> str:
        """``***`` for a secret, ``NO_SHIPPED_DEFAULT`` when the file carries no entry."""
        return self.control.shipped_default

    @property
    def has_shipped_default(self) -> bool:
        return self.control.has_shipped_default

    @property
    def is_secret(self) -> bool:
        return self.control.is_secret

    @property
    def is_safety_posture(self) -> bool:
        return self.control.is_safety_posture

    @property
    def choices(self) -> tuple[SettingChoice, ...]:
        """Non-empty -> the cells render as selects."""
        return self.control.choices

    @property
    def drifts(self) -> bool:
        """Whether ANY scope differs from the shipped default — counted once per setting."""
        return any(not cell.matches_default for cell in self.cells)


@dataclass(frozen=True, slots=True)
class SettingsSection:
    """One entry of the left nav — a leaf group, addressable on its own."""

    label: str
    path: tuple[str, ...]
    slug: str
    key_count: int
    drift_count: int = 0

    @property
    def parent_label(self) -> str:
        """The levels above this leaf, for a nav that shows where a section sits."""
        return " / ".join(self.path[:-1])


@dataclass(frozen=True, slots=True)
class SettingsGroupView:
    """The right pane — one section's rows, or a visible error (never a 500)."""

    section: SettingsSection | None = None
    settings: tuple[EditableSetting, ...] = ()
    scopes: tuple[str, ...] = (GLOBAL_SCOPE,)
    error: str = ""

    @property
    def scope_labels(self) -> tuple[str, ...]:
        return tuple(scope or GLOBAL_LABEL for scope in self.scopes)


@dataclass(frozen=True, slots=True)
class SettingsEditorView:
    """The page frame — the nav and the selected section's pane."""

    sections: tuple[SettingsSection, ...] = ()
    group: SettingsGroupView = SettingsGroupView()
    error: str = ""


@dataclass(frozen=True, slots=True)
class _Grid:
    """Every scope's resolution of every requested key, read once for the whole page."""

    scopes: tuple[str, ...]
    resolved: Mapping[str, Mapping[str, ResolvedSetting]]
    shipped: Mapping[str, ConfigValue]

    def row(self, key: str) -> EditableSetting:
        control = SettingControl(key, self.shipped)
        return EditableSetting(
            control=control,
            cells=tuple(self._cell(control, scope) for scope in self.scopes),
        )

    def _cell(self, control: SettingControl, scope: str) -> ScopeCell:
        resolved = self.resolved[scope][control.key]
        value = control.display_value(resolved.value)
        # Compared as the operator SEES them: identical text in the cell means identical
        # value. A key the shipped file does not carry has no shipped text to equal, so it
        # falls back to whether an operator's own tier supplied it at all — a Secret/Personal
        # key with no entry in defaults.toml still has a real default (its code default), and
        # an env/DB tier outranking that IS the drift the grid exists to surface.
        matches = not resolved.is_overridden or (control.has_shipped_default and value == control.shipped_default)
        return ScopeCell(
            key=control.key,
            scope=scope,
            value=value,
            selected=control.wire_value(resolved.value),
            source=resolved.source.value,
            matches_default=matches,
        )


def _build_grid(keys: Sequence[str]) -> _Grid:
    scopes = available_scopes()
    return _Grid(
        scopes=scopes,
        resolved={scope: resolve_settings(keys, scope=scope) for scope in scopes},
        shipped=shipped_defaults_table(),
    )


def _leaves() -> tuple[SettingGroupNode[str], ...]:
    """The schema's key names partitioned into the group tree's leaves, in render order."""
    return group_leaves(group_tree(sorted(TeatreeSettingsSchema.model_fields), key_of=lambda key: key))


def _section(leaf: SettingGroupNode[str], drift_count: int = 0) -> SettingsSection:
    return SettingsSection(
        label=leaf.label,
        path=leaf.path,
        slug=group_slug(leaf.path),
        key_count=len(leaf.rows),
        drift_count=drift_count,
    )


def build_settings_sections() -> tuple[SettingsSection, ...]:
    """The left nav WITHOUT drift counts — the cheap shape, for callers that need paths only.

    Built from the key NAMES alone, so listing it costs no value resolution. The partition
    is total, so every schema key is reachable through exactly one entry.
    """
    return tuple(_section(leaf) for leaf in _leaves())


def build_settings_group(slug: str = "") -> SettingsGroupView:
    """One section's grid rows; the first section when *slug* names none."""
    leaves = {group_slug(leaf.path): leaf for leaf in _leaves()}
    leaf = leaves.get(slug) or next(iter(leaves.values()), None)
    if leaf is None:
        return SettingsGroupView(error="settings unavailable — the schema declares no groups")
    try:
        grid = _build_grid(leaf.rows)
    except Exception:
        logger.warning("dash settings group read failed — degrading to an error pane", exc_info=True)
        return SettingsGroupView(section=_section(leaf), error="settings unavailable — read failed")
    rows = tuple(grid.row(key) for key in leaf.rows)
    return SettingsGroupView(
        section=_section(leaf, drift_count=sum(1 for row in rows if row.drifts)),
        settings=rows,
        scopes=grid.scopes,
    )


def build_settings_editor(slug: str = "") -> SettingsEditorView:
    """The whole page — the nav with its per-section drift counts, and the selected pane.

    The nav's counts need every key resolved in every scope, so the grid is built ONCE over
    the whole schema and both the counts and the selected section's rows are read out of it.
    That keeps the page's cost the number of SCOPES, not the number of settings.
    """
    try:
        leaves = _leaves()
        grid = _build_grid(sorted(TeatreeSettingsSchema.model_fields))
    except Exception:
        logger.warning("dash settings editor read failed — degrading to an error page", exc_info=True)
        return SettingsEditorView(error="settings unavailable — read failed")
    if not leaves:
        return SettingsEditorView(error="settings unavailable — the schema declares no groups")
    rows_by_path = {leaf.path: tuple(grid.row(key) for key in leaf.rows) for leaf in leaves}
    sections = tuple(_section(leaf, sum(1 for row in rows_by_path[leaf.path] if row.drifts)) for leaf in leaves)
    selected = next((section for section in sections if section.slug == slug), sections[0])
    return SettingsEditorView(
        sections=sections,
        group=SettingsGroupView(section=selected, settings=rows_by_path[selected.path], scopes=grid.scopes),
    )


def available_scopes() -> tuple[str, ...]:
    """Global first, then every overlay scope the operator can edit — the grid's COLUMNS.

    The union of the registered overlays and the scopes that already hold rows, so a
    scope written by ``config_setting set --overlay`` before its overlay was registered
    (or after it was uninstalled) is still visible rather than stranded.
    """
    try:
        stored = set(ConfigSetting.objects.exclude(scope=GLOBAL_SCOPE).values_list("scope", flat=True).distinct())
    except Exception:
        # The rest of the page degrades rather than 500s when a tier cannot be read, and the
        # column list is no exception: global alone still renders every setting.
        logger.warning("scope read failed — offering the global column alone", exc_info=True)
        stored = set()
    try:
        registered = set(get_all_overlays())
    except Exception:
        logger.warning("overlay discovery failed — offering only the scopes holding rows", exc_info=True)
        registered = set()
    return (GLOBAL_SCOPE, *sorted(stored | registered))


def build_setting_row(key: str) -> EditableSetting:
    """One row, re-read after a write — the htmx swap unit, masked by the same policy.

    The whole ROW is the swap unit because a global write changes what every overlay column
    inherits; swapping the edited cell alone would leave the others showing a stale value.
    """
    return _build_grid([key]).row(key)


__all__ = [
    "GLOBAL_LABEL",
    "MASKED",
    "EditableSetting",
    "ScopeCell",
    "SettingChoice",
    "SettingsEditorView",
    "SettingsGroupView",
    "SettingsSection",
    "ValueSource",
    "available_scopes",
    "build_setting_row",
    "build_settings_editor",
    "build_settings_group",
    "build_settings_sections",
]
