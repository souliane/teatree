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
with ``***`` HERE, before the row enters the view context. Export withholds secrets and keeps
personal; import previews via ``import_toml_to_db(dry_run=True)``.

Help text and the constrained-value options are both DERIVED, never re-typed:
:func:`~teatree.config.setting_help.setting_help` is the same sentence ``defaults.toml``
carries as that key's comment, and :func:`~teatree.config.schema.setting_choices` is the
schema's own admissible set — so a select can never offer a value the validator refuses.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from django.urls import reverse

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.provenance import ResolvedSetting, ValueSource, resolve_settings
from teatree.config.schema import TeatreeSettingsSchema, setting_choices
from teatree.config.setting_groups import SettingGroupNode, group_leaves, group_slug, group_tree
from teatree.config.setting_help import setting_help
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.core.config_display import MASKED, is_secret, render_value
from teatree.core.config_migration import ConfigImport, export_db_to_toml, import_toml_to_db
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigValue
from teatree.core.overlay_loader import get_all_overlays

logger = logging.getLogger(__name__)

#: The column header the global scope renders under — every other column is an overlay name.
GLOBAL_LABEL = "global"


def _wire(value: object) -> str:
    """*value* as the JSON literal an edit POSTs — the one encoding both ends agree on."""
    return json.dumps(value, default=str)


@dataclass(frozen=True, slots=True)
class SettingChoice:
    """One option of a constrained control — the JSON an edit posts, and its screen label.

    The label runs through the SAME ``render_value`` every other value on the page does, so
    a boolean reads ``on`` / ``off`` in the select exactly as it does in the default column.
    """

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ScopeCell:
    """One scope's value for one setting — the editable unit of the grid."""

    key: str
    scope: str  # "" is global
    value: str  # ``***`` for a secret, else the effective value as display text
    selected: str  # the JSON literal of the effective value — what a control holds and posts
    source: str  # which tier of the resolution chain supplied it — the cell's tooltip
    matches_default: bool  # green when true, brown when the operator has changed it

    @property
    def label(self) -> str:
        return self.scope or GLOBAL_LABEL

    @property
    def post_url(self) -> str:
        """Where this cell's edit goes — the key in the path, its own scope in the query."""
        url = reverse("dash:settings_set", args=[self.key])
        return f"{url}?scope={quote(self.scope)}" if self.scope else url


@dataclass(frozen=True, slots=True)
class EditableSetting:
    """One ROW of the grid — the setting, its shipped default, and every scope's value."""

    name: str
    help_text: str
    shipped_default: str  # ``***`` for a secret, "" when the shipped file carries none
    has_shipped_default: bool
    is_secret: bool
    is_safety_posture: bool
    choices: tuple[SettingChoice, ...]  # non-empty -> the cells render as selects
    cells: tuple[ScopeCell, ...]

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
        default = _display_default(key, self.shipped)
        return EditableSetting(
            name=key,
            help_text=setting_help(key),
            shipped_default=default,
            has_shipped_default=key in self.shipped,
            is_secret=is_secret(key),
            is_safety_posture=key in SAFETY_POSTURE_KEYS,
            choices=_choices(key),
            cells=tuple(self._cell(scope, key, default) for scope in self.scopes),
        )

    def _cell(self, scope: str, key: str, default: str) -> ScopeCell:
        resolved = self.resolved[scope][key]
        value = _display_value(key, resolved)
        # Compared as the operator SEES them: identical text in the cell means identical
        # value. A key the shipped file does not carry has no default to differ from.
        matches = not resolved.is_overridden or value == default or key not in self.shipped
        return ScopeCell(
            key=key,
            scope=scope,
            value=value,
            # The wire value is masked by the SAME test as the display value: it is a second
            # rendering of the same stored value, and a control that held the real one would
            # put a secret in the page the moment a template read it.
            selected=MASKED if is_secret(key) else _wire(resolved.value),
            source=resolved.source.value,
            matches_default=matches,
        )


def _choices(key: str) -> tuple[SettingChoice, ...]:
    """*key*'s schema-admissible values as select options — derived, never hand-listed."""
    return tuple(SettingChoice(_wire(value), render_value(value)) for value in setting_choices(key))


def _display_value(key: str, resolved: ResolvedSetting) -> str:
    """The cell's shown value — ``***`` for a secret, else the resolved value as text.

    A secret returns ``MASKED`` WITHOUT reading the resolved value, so a stored secret can
    never be serialised into the page.
    """
    return MASKED if is_secret(key) else render_value(resolved.value)


def _display_default(key: str, shipped: Mapping[str, ConfigValue]) -> str:
    """The shipped default as display text — ``***`` for a secret, "" when there is none.

    Read from the shipped TABLE, in the same stored form the cells render, so "same as
    default" compares like with like rather than a stored scalar against a coerced value.
    """
    if is_secret(key):
        return MASKED
    return render_value(shipped[key]) if key in shipped else ""


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


def export_text(*, default_keys_only: bool = False, include_defaults: bool = False) -> str:
    """The shareable export dump — secrets withheld, personal kept (Phase-4 semantics).

    The two filters are the page's two checkboxes, both unticked by default so the plain
    download is the delta dump it has always been. Ticking both yields the ``defaults.toml``
    shape: a complete, drop-in replacement for the shipped file.
    """
    return export_db_to_toml(
        include_private=False,
        default_keys_only=default_keys_only,
        include_defaults=include_defaults,
    ).toml


def import_preview(text: str) -> ConfigImport:
    """Classify an import WITHOUT writing — the dry-run preview of what would change.

    Classifies as if the safety-posture keys were authorized so the preview can SHOW and flag
    them; nothing is written, and the apply path re-runs the classification with the operator's
    actual authorization.
    """
    return import_toml_to_db(text, dry_run=True, allow_safety_posture=True)


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
    "export_text",
    "import_preview",
]
