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

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.provenance import ResolvedSetting, resolve_settings
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import SettingGroupNode, group_leaves, group_slug, group_tree
from teatree.config.settings import OverlayEntry
from teatree.core.config_display import MASKED
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigValue
from teatree.core.overlay_loader import get_all_overlays
from teatree.core.setting_cell import (
    GLOBAL_LABEL,
    DriftVerdict,
    SettingCell,
    env_pin_refusal,
    settings_write_url,
    verdict_for,
)
from teatree.core.setting_control import SettingChoice, SettingControl

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EditableSetting:
    """One ROW of the grid — the setting's control, and every scope's value for it.

    The per-setting half is COMPOSED, not restated: the control is the same
    :class:`~teatree.core.setting_control.SettingControl` the snapshot surface derives from,
    delegated field by field so the row template and its readers keep one vocabulary.
    """

    control: SettingControl
    cells: tuple[SettingCell, ...]

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
    def governance(self) -> tuple[str, ...]:
        """The key's governance classes, so the row says WHAT is being flipped (B12)."""
        return self.control.governance

    @property
    def choices(self) -> tuple[SettingChoice, ...]:
        """Non-empty -> the cells render as selects."""
        return self.control.choices

    @property
    def drifts(self) -> bool:
        """Whether ANY scope differs from the shipped default — counted once per setting."""
        return any(cell.drifts for cell in self.cells)


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

    def _cell(self, control: SettingControl, scope: str) -> SettingCell:
        resolved = self.resolved[scope][control.key]
        value = control.display_value(resolved.value)
        return SettingCell(
            key=control.key,
            scope=scope,
            value=value,
            selected=control.wire_value(resolved.value),
            source=resolved.source.value,
            verdict=verdict_for(control, overridden=resolved.is_overridden, value=value),
            unwritable_reason=env_pin_refusal(control.key, resolved.source.value),
            post_url=settings_write_url(control.key, scope),
        )


def _build_grid(keys: Sequence[str], columns: Sequence[str] | None = None) -> _Grid:
    """Every column's resolution of *keys*; this box's own scopes when *columns* names none.

    *columns* is the seam a grid comparing BOXES supplies its own dimension through — the row
    half is identical, so only the columns and where their values come from differ.
    """
    scopes = tuple(columns) if columns is not None else available_scopes()
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


def _stored_scopes() -> set[str]:
    """Every non-global scope that already holds a row, or ``set()`` when the tier is unreadable.

    The rest of the page degrades rather than 500s when a tier cannot be read, and the column
    list is no exception: global alone still renders every setting.
    """
    try:
        return set(ConfigSetting.objects.exclude(scope=GLOBAL_SCOPE).values_list("scope", flat=True).distinct())
    except Exception:
        logger.warning("scope read failed — offering the global column alone", exc_info=True)
        return set()


def _registered_overlays() -> set[str]:
    try:
        return set(get_all_overlays())
    except Exception:
        logger.warning("overlay discovery failed — offering only the scopes holding rows", exc_info=True)
        return set()


def _column_spelling(spellings: set[str], registered: set[str]) -> str:
    """Which of one overlay's spellings names its column — the REGISTERED one wherever there is one.

    Not cosmetic. A column resolves its tiers with its own name as the exact-match spelling
    (:func:`~teatree.config.override_reader.load_overlay_rows` applies the alias group first
    and the exact name last), and the process reads its rows under the name the overlay is
    registered as — so the registered spelling is the column whose reading is the one actually
    in force. Naming the column after the alias would show a value the box does not use.
    """
    return next(iter(sorted(spellings & registered)), min(spellings))


def scopes_of_column(scope: str) -> tuple[str, ...]:
    """Every stored scope the *scope* column speaks for — itself, then its other spellings.

    A column is an OVERLAY, and the resolver merges every canonically-equivalent scope into
    that overlay's tier. So a clear that removed only the exact spelling would leave an alias
    row still supplying the value: restore-to-default would report success and change nothing.
    The global column speaks for itself alone — it is not an overlay and folds onto nothing.
    """
    if not scope:
        return (GLOBAL_SCOPE,)
    canonical = OverlayEntry.canonical_overlay_name(scope)
    others = (one for one in _stored_scopes() if one != scope)
    return (scope, *sorted(one for one in others if OverlayEntry.canonical_overlay_name(one) == canonical))


def available_scopes() -> tuple[str, ...]:
    """Global first, then ONE column per overlay the operator can edit — the grid's COLUMNS.

    The union of the registered overlays and the scopes that already hold rows, so a scope
    written by ``config_setting set --overlay`` before its overlay was registered (or after it
    was uninstalled) is still visible rather than stranded.

    Folded onto the canonical key :func:`~teatree.config.override_reader.load_overlay_rows`
    already merges rows by, so an overlay holding rows under BOTH its bare alias and its
    ``t3-`` entry-point name is one column rather than two. Unfolded, the page rendered that
    overlay twice under two names showing one merged reading — and neither column said which
    spelling a write would land in.
    """
    registered = _registered_overlays()
    spellings: dict[str, set[str]] = {}
    for scope in _stored_scopes() | registered:
        spellings.setdefault(OverlayEntry.canonical_overlay_name(scope), set()).add(scope)
    return (GLOBAL_SCOPE, *sorted(_column_spelling(group, registered) for group in spellings.values()))


def build_setting_row(key: str) -> EditableSetting:
    """One row, re-read after a write — the htmx swap unit, masked by the same policy.

    The whole ROW is the swap unit because a global write changes what every overlay column
    inherits; swapping the edited cell alone would leave the others showing a stale value.
    """
    return _build_grid([key]).row(key)


__all__ = [
    "GLOBAL_LABEL",
    "MASKED",
    "DriftVerdict",
    "EditableSetting",
    "SettingCell",
    "SettingChoice",
    "SettingsEditorView",
    "SettingsGroupView",
    "SettingsSection",
    "available_scopes",
    "build_setting_row",
    "build_settings_editor",
    "build_settings_group",
    "build_settings_sections",
    "scopes_of_column",
]
