"""Pure planner for a DB→``defaults.toml`` snapshot — proposes, never writes.

``defaults.toml`` is the hand-editable authority for the shipped default VALUES, so
this planner treats the CURRENT file as its BASE and the live GLOBAL-scope
``ConfigSetting`` rows as the PROPOSED changes on top of it. A hand-edited value the
live box does not override therefore survives a snapshot run untouched — the file is
never re-derived from the in-code dataclass defaults.

Nothing here writes. :func:`plan_snapshot` returns the proposed file text plus the
per-key change list; the owner-approval gate and the file write live in the management
command (:mod:`teatree.core.management.commands.snapshot_settings_defaults`), so this
module stays in the ``config`` layer and is unit-testable with plain dicts.

Exclusions. SAFETY-posture keys and DARK feature-flags can never move through this path
— approval or not — because a write to one is an authorization, never a shipped default.
Owner-workflow / engagement keys (:data:`WORKFLOW_ENGAGEMENT_KEYS`) are declined too: the
live box has turned them on for its own operation, which is wrong for a fresh install.
SECRET / PERSONAL rows are never emitted (their empty code default stays in the model);
overlay-scope rows and stale/retired keys are reported, never emitted.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.feature_flags import dark_flags
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.schema import Category, setting_meta
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS

# A stored config value — the JSON/TOML shapes a ConfigSetting row round-trips.
type SettingValue = bool | int | float | str | list[object] | dict[str, object]

#: Owner-workflow + engagement keys kept at the current shipped value even when the live
#: box overrides them (O2). The first row is the owner-named workflow set; the second is
#: the loop / engagement enable-toggles (and their inert config) — a fresh install must
#: not auto-start autonomous work, so these never ship the live "turned on" value.
WORKFLOW_ENGAGEMENT_KEYS: frozenset[str] = frozenset(
    {
        "wip",
        "mode",
        "autoload",
        "contribute",
        "agent_runtime",
        "issue_implementer_enabled",
        "issue_implementer_label",
        "triage_assessor_enabled",
        "active_loop_schedule",
    }
)

#: The two dict-valued DEFAULT keys that render as their own ``[teatree.<name>]``
#: sub-table rather than an inline value in the ``[teatree]`` table.
_SUBTABLE_KEYS: tuple[str, ...] = ("mr_reminder", "speak")

_ABSENT = "(absent)"

_HEADER = """\
# teatree shipped config defaults — the DEFAULT-category keys at their ship values.
#
# HAND-EDITABLE. Edit a value here and the resolver serves it: this file is the last
# tier of every resolution chain (env -> DB(overlay) -> DB(global) -> overlay code
# default -> THIS FILE). A value that diverges from its in-code dataclass default needs
# a matching entry in `defaults_approvals.toml` — that entry is the reviewed decision,
# and CI refuses an unrecorded divergence.
#
# `manage.py snapshot_settings_defaults` proposes a snapshot of the live box's global
# settings onto this file; it renders the diff, asks the owner through the deferred-
# question queue, and writes only once that question is answered `approve`.
#
# Shape: the `config_setting export`/`import` schema — a `[teatree]` table with
# `speak`/`mr_reminder` as sub-tables. EXACTLY the Category.DEFAULT keys of
# `teatree.config.schema.TeatreeSettingsSchema` appear here; PERSONAL/SECRET keys
# (operator identifiers, machine paths, model-routing tables, customer/brand terms,
# credential coordinates) are ABSENT by construction and resolve from their empty
# code defaults.
#
# Safety-posture keys and dark feature-flags are pinned to their fail-closed/off value
# and can never move through the snapshot path, approval or not.
"""

# The scan takes the `"<key> <json-value>"` text and returns the first matched
# banned term (or None). Injected so `config` never imports `hooks` (the layer
# above it); the command binds it to `hooks.term_match.matched_term`.
BannedScan = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class SnapshotChange:
    """One proposed edit to the shipped file — what it is now, what it would become."""

    key: str
    shipped: SettingValue | None  # ``None`` when the file does not carry the key yet
    proposed: SettingValue
    scope: str  # "global" (a live DB row) | "code-default" (a key the file is missing)


@dataclass(frozen=True, slots=True)
class DeclinedChange:
    """A live override this path refuses to snapshot, and why."""

    key: str
    reason: str


@dataclass(frozen=True)
class SnapshotPlan:
    """The proposed file plus everything the run decided, for the owner to review."""

    toml: str
    changes: tuple[SnapshotChange, ...] = ()
    declined: tuple[DeclinedChange, ...] = ()
    skipped_secret: tuple[str, ...] = ()
    skipped_personal: tuple[str, ...] = ()
    stale_keys: tuple[str, ...] = ()
    overlay_scope_rows: tuple[tuple[str, str], ...] = ()
    dropped_keys: tuple[str, ...] = ()


@dataclass
class _Reported:
    """Mutable accumulator for the non-DEFAULT live rows, folded into the plan at the end."""

    skipped_secret: list[str] = field(default_factory=list)
    skipped_personal: list[str] = field(default_factory=list)
    stale_keys: list[str] = field(default_factory=list)


def conservative_keys() -> frozenset[str]:
    """The keys that ALWAYS keep the shipped value, never the live override."""
    return pinned_fail_closed_keys() | WORKFLOW_ENGAGEMENT_KEYS


def pinned_fail_closed_keys() -> frozenset[str]:
    """Safety-posture + dark-flag keys — never movable by ANY path, approval or not."""
    return SAFETY_POSTURE_KEYS | frozenset(dark_flags())


def default_category_keys() -> frozenset[str]:
    """Exactly the keys the shipped file carries."""
    return frozenset(k for k in ALL_KNOWN_CONFIG_SETTINGS if setting_meta(k).category is Category.DEFAULT)


def _decline_reason(key: str) -> str:
    if key in SAFETY_POSTURE_KEYS:
        return "safety-posture"
    if key in dark_flags():
        return "dark-flag"
    return "workflow-engagement"


def _classify_non_default(live_global: dict[str, SettingValue], reported: _Reported) -> None:
    """File every non-DEFAULT live global row into the report (never emitted)."""
    for key in sorted(live_global):
        if key not in ALL_KNOWN_CONFIG_SETTINGS:
            reported.stale_keys.append(key)
            continue
        category = setting_meta(key).category
        if category is Category.SECRET:
            reported.skipped_secret.append(key)
        elif category is Category.PERSONAL:
            reported.skipped_personal.append(key)


def _decide(
    key: str,
    shipped: SettingValue | None,
    live: SettingValue,
    banned_scan: BannedScan,
) -> SnapshotChange | DeclinedChange | None:
    """The snapshot decision for one DEFAULT key carrying a live global row."""
    if key in conservative_keys():
        return DeclinedChange(key, _decline_reason(key))
    try:
        coerced = ALL_KNOWN_CONFIG_SETTINGS[key](live)
    except Exception:  # noqa: BLE001 — a malformed live row is declined, never fatal
        return DeclinedChange(key, "uncoercible")
    hit = banned_scan(f"{key} {json.dumps(coerced, default=str)}")
    if hit is not None:
        return DeclinedChange(key, f"banned-term:{hit}")
    if coerced == shipped:
        return None
    return SnapshotChange(key=key, shipped=shipped, proposed=coerced, scope="global")


def plan_snapshot(
    *,
    shipped: dict[str, SettingValue],
    code_defaults: dict[str, SettingValue],
    live_global: dict[str, SettingValue],
    overlay_scope_rows: list[tuple[str, str]],
    banned_scan: BannedScan,
) -> SnapshotPlan:
    """Propose the live box's global settings onto the CURRENT shipped file.

    *shipped* is the file's ``[teatree]`` table as it stands (the base). *code_defaults*
    supplies a value for any DEFAULT key the file does not carry yet, so the emitted file
    stays exhaustive. *live_global* is every GLOBAL-scope ``ConfigSetting`` row (any
    category); *overlay_scope_rows* is ``(scope, key)`` for every non-global row (reported,
    never emitted). *banned_scan* returns the first banned term hit in a ``"<key> <value>"``
    text.
    """
    reported = _Reported()
    _classify_non_default(live_global, reported)

    emitted = {key: shipped.get(key, code_defaults[key]) for key in default_category_keys()}
    changes: list[SnapshotChange] = []
    declined: list[DeclinedChange] = []
    for key in sorted(emitted):
        if key not in shipped:
            changes.append(SnapshotChange(key=key, shipped=None, proposed=emitted[key], scope="code-default"))
        if key not in live_global:
            continue
        decision = _decide(key, shipped.get(key), live_global[key], banned_scan)
        if isinstance(decision, DeclinedChange):
            declined.append(decision)
        elif isinstance(decision, SnapshotChange):
            changes.append(decision)
            emitted[key] = decision.proposed

    return SnapshotPlan(
        toml=render_toml(emitted),
        changes=tuple(changes),
        declined=tuple(declined),
        skipped_secret=tuple(reported.skipped_secret),
        skipped_personal=tuple(reported.skipped_personal),
        stale_keys=tuple(reported.stale_keys),
        overlay_scope_rows=tuple(sorted(overlay_scope_rows)),
        dropped_keys=tuple(sorted(set(shipped) - set(emitted))),
    )


def plan_fingerprint(changes: tuple[SnapshotChange, ...]) -> str:
    """A stable digest of the exact change set, so an approval binds to ONE diff.

    An approval recorded against a rendered diff must not authorize a different diff the
    box produced later; the command carries this digest on the question and re-derives it
    before writing.
    """
    payload = json.dumps(
        [[c.key, c.shipped, c.proposed, c.scope] for c in sorted(changes, key=lambda c: c.key)],
        default=str,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def change_table(changes: tuple[SnapshotChange, ...]) -> tuple[list[str], list[list[str]]]:
    """The proposed diff as table headers + rows, for a Slack table or a CLI table."""
    rows = [
        [c.key, _ABSENT if c.shipped is None else str(c.shipped), str(c.proposed), c.scope]
        for c in sorted(changes, key=lambda c: c.key)
    ]
    return ["setting", "shipped now", "proposed", "scope"], rows


def render_toml(emitted: dict[str, SettingValue]) -> str:
    """Render *emitted* into the canonical ``[teatree]`` + sub-table TOML text."""
    document = tomlkit.document()
    teatree = tomlkit.table()
    for key in sorted(k for k in emitted if k not in _SUBTABLE_KEYS):
        teatree[key] = emitted[key]
    for name in _SUBTABLE_KEYS:
        teatree[name] = _nested_table(cast("dict[str, SettingValue]", emitted[name]))
    document["teatree"] = teatree
    return _HEADER + "\n" + tomlkit.dumps(document)


def _nested_table(value: dict[str, SettingValue]) -> tomlkit_items.Table:
    """A ``dict``-valued setting rendered as a nested TOML table (channels -> sub-table)."""
    table = tomlkit.table()
    for key in sorted(value):
        inner = value[key]
        table[key] = _nested_table(cast("dict[str, SettingValue]", inner)) if isinstance(inner, dict) else inner
    return table
