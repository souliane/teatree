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

Exclusions. Every key :func:`pinned_fail_closed_keys` names can never move through this
path — approval or not — because a write to one is a posture, never a shipped default.
Owner-workflow / engagement keys (:data:`WORKFLOW_ENGAGEMENT_KEYS`) are declined too: the
live box has turned them on for its own operation, which is wrong for a fresh install.
SECRET / PERSONAL rows are never emitted (their empty code default stays in the model);
overlay-scope rows and stale/retired keys are reported, never emitted.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch

import tomlkit

from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.feature_flags import FEATURE_FLAGS
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.registries import COLD_SETTINGS, REGISTRY_KEYS
from teatree.config.schema import Category, setting_meta
from teatree.config.setting_groups import grouped_settings_table
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
        "mr_triage_enabled",
        "active_loop_schedule",
    }
)

#: Name-shaped safety wires — a gate kill-switch and the opt-in ``require_*`` training
#: wheels. Mirrors ``teatree.mcp.write_tools._REFUSED_KEY_GLOBS``; the two are held equal
#: by ``tests/config/test_defaults_snapshot.py``'s superset pin rather than by an import,
#: because ``config`` sits below ``mcp`` and may not reach up to it.
_SAFETY_KEY_GLOBS: tuple[str, ...] = ("*_gate_enabled", "require_*")

_ABSENT = "(absent)"

_HEADER = """\
# teatree shipped defaults — every value a fresh install starts from.
#
# HAND-EDITABLE. Edit a value here and the box serves it. `[teatree]` is the last tier of
# every settings resolution chain (env -> DB(overlay) -> DB(global) -> overlay code
# default -> THIS FILE); the seed tables below are what `t3 setup` creates the loop, mode
# and schedule rows from. A `[teatree]` value that diverges from its in-code dataclass
# default needs a matching entry in `defaults_approvals.toml` — that entry is the reviewed
# decision, and CI refuses an unrecorded divergence.
#
# `manage.py snapshot_settings_defaults` proposes a snapshot of the live box's global
# settings onto the `[teatree]` table; it renders the diff, asks the owner through the
# deferred-question queue, and writes only once that question is answered `approve`. It
# rewrites `[teatree]` alone — every seed table below is hand-maintained.
#
# `[teatree]` — the `config_setting export`/`import` schema, with `speak`/`mr_reminder` as
# sub-tables. EXACTLY the Category.DEFAULT keys of
# `teatree.config.schema.TeatreeSettingsSchema` appear here; PERSONAL/SECRET keys
# (operator identifiers, machine paths, model-routing tables, customer/brand terms,
# credential coordinates) are ABSENT by construction and resolve from their empty
# code defaults. Safety-posture keys and dark feature-flags are pinned to their
# fail-closed/off value and can never move through the snapshot path, approval or not.
# The declaration hierarchy renders as real NESTED sub-tables — the same tree the dashboard
# renders and the export dump emits. The KEY NAMESPACE stays flat: that namespace is the
# persisted contract every reader, env override and cold sqlite3 read depends on, and
# `config/cold_defaults.py` flattens the group wrappers back on read. A sub-table named
# after a declared setting (`speak`, `mr_reminder`) is a setting; any other is a group.
# Put a new key under its group's table; CI refuses one sitting outside its group.
# Each key's trailing comment says what it ACCEPTS — the stored type, plus the alternatives
# where the schema constrains them to a set — then what it means. Both halves are DERIVED
# (`config/setting_annotation.py`, `config/setting_help.py`), so editing one here is
# overwritten by the next render.
#
# `[loops.<name>]` — the autonomous loops that ship: `delay_seconds` (tick cadence),
# optional `daily_at` for a once-per-day loop, `colleague_facing` (the away-gate skips
# it), `default_enabled` (only the local/read-only operational core ships ON),
# `description`, and `prompt_body` for the one prompt-backed loop (every other loop runs
# its own `src/teatree/loops/<name>/loop.py`). Table ORDER is the seed order, pinned
# against the frozen `0001_initial` copy.
#
# `[modes.<name>]` — a curated mode: its availability posture plus an `entries` table
# masking each loop on/off. A loop ABSENT from `entries` INHERITS its own enabled flag,
# which is how a destructive-capable loop is never silently re-enabled by a mode switch.
# That inheritance is why a mode masking DELIVERY off (`ship` / `tickets`) must also name
# the INTAKE loop (`issue_implementer`): left absent it keeps claiming issues the masked
# delivery lane cannot merge. `teatree.loops.mode_shape` fails the audit on that shape.
# The mirror rule is the LOAD-BEARING tier (`teatree.loops.mode_shape.LOAD_BEARING_LOOPS`):
# no mask may quiet it (the low-power mode excepted), and none may keep `db_backup` writing
# once every reclaim loop is quiet — that shape can only ever consume disk.
#
# `[schedules.<name>]` — a weekly calendar of `[[...slots]]`, each a wall-clock start in
# the schedule's `timezone` (`days` are Python weekday numbers, Monday = 0).
#
# The seed is `get_or_create` by name: editing a seed table changes what a FRESH install
# gets and never overwrites a row an operator already edited on a live box. So a DELETED
# shipped row is the recoverable failure — `t3 setup` puts it back. The one that is not is a
# row sitting present and INERT: `t3 loops audit` reads the seed tables below as the expected
# set (the DB cannot answer for a row that is gone) and names every shipped definition that
# is missing, disabled against its shipped flag, or not ticking. It also names every live
# mode mask and calendar whose VALUE has diverged from what ships here — reported as a note
# with both values, never rewritten, because the override may well be deliberate. Deleting a
# shipped definition needs a typed `stop-<name>` naming what stops.
"""

# The scan takes the `"<key> <json-value>"` text and returns the first matched
# banned term (or None). Injected so `config` never imports `hooks` (the layer
# above it); the command binds it to `hooks.term_match.matched_term`.
BannedScan = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class ShippedFile:
    """The shipped file as the planner sees it — the base a snapshot edits.

    *table* is the ``[teatree]`` table as it stands; *text* is the whole file, so the
    sibling seed tables and the hand-written comments survive the re-rendered ``[teatree]``
    (empty text builds the document from scratch, header included). *code_defaults*
    supplies a value for any DEFAULT key the file does not carry yet, so the emitted file
    stays exhaustive — it defaults to *table*, which is what a complete file already holds.
    """

    table: dict[str, SettingValue]
    text: str = ""
    _code_defaults: dict[str, SettingValue] | None = None

    @property
    def code_defaults(self) -> dict[str, SettingValue]:
        return self.table if self._code_defaults is None else self._code_defaults


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
    """Keys never movable by ANY path, approval or not — the write is a posture, not a default.

    A key too dangerous for an agent to flip over MCP is too dangerous to bake into every
    fresh install, so this set is held a SUPERSET of everything
    ``teatree.mcp.write_tools.refuse_reason`` refuses. It is restated from the config-layer
    registries rather than imported, because ``config`` sits below ``mcp``.

    The classes: safety-posture keys, every feature flag (its value is code-governed and
    dies with the code it gates — a SETTLING flag an operator turned off during a soak is
    no more a shipped default than a DARK one), the pre-Django cold-hook gate wires, the
    definition registries, every cold-read key (the master ``danger_gate_fail_open``
    fail-open switch among them), and the name-shaped safety wires.
    """
    return (
        SAFETY_POSTURE_KEYS
        | frozenset(FEATURE_FLAGS)
        | frozenset(COLD_HOOK_SETTINGS)
        | frozenset(REGISTRY_KEYS)
        | frozenset(COLD_SETTINGS)
        | frozenset(k for k in ALL_KNOWN_CONFIG_SETTINGS if _matches_safety_glob(k))
    )


def _matches_safety_glob(key: str) -> bool:
    return any(fnmatch(key, glob) for glob in _SAFETY_KEY_GLOBS)


def default_category_keys() -> frozenset[str]:
    """Exactly the keys the shipped file carries."""
    return frozenset(k for k in ALL_KNOWN_CONFIG_SETTINGS if setting_meta(k).category is Category.DEFAULT)


def _decline_reason(key: str) -> str:
    """The narrowest class that pins *key*, so the owner sees WHY the row was refused."""
    lanes: tuple[tuple[bool, str], ...] = (
        (key in SAFETY_POSTURE_KEYS, "safety-posture"),
        (key in FEATURE_FLAGS, "feature-flag"),
        (key in COLD_HOOK_SETTINGS, "cold-hook-gate"),
        (key in REGISTRY_KEYS, "definition-registry"),
        (key in COLD_SETTINGS, "cold-read"),
        (_matches_safety_glob(key), "safety-gate"),
    )
    for matched, reason in lanes:
        if matched:
            return reason
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
    shipped: "ShippedFile",
    live_global: dict[str, SettingValue],
    overlay_scope_rows: list[tuple[str, str]],
    banned_scan: BannedScan,
) -> SnapshotPlan:
    """Propose the live box's global settings onto the CURRENT shipped file.

    *shipped* is the file as it stands — the base this plan edits. *live_global* is every
    GLOBAL-scope ``ConfigSetting`` row (any category); *overlay_scope_rows* is
    ``(scope, key)`` for every non-global row (reported, never emitted). *banned_scan*
    returns the first banned term hit in a ``"<key> <value>"`` text.
    """
    reported = _Reported()
    _classify_non_default(live_global, reported)

    table, code_defaults = shipped.table, shipped.code_defaults
    emitted = {key: table.get(key, code_defaults[key]) for key in default_category_keys()}
    changes: list[SnapshotChange] = []
    declined: list[DeclinedChange] = []
    for key in sorted(emitted):
        if key not in table:
            changes.append(SnapshotChange(key=key, shipped=None, proposed=emitted[key], scope="code-default"))
        if key not in live_global:
            continue
        decision = _decide(key, table.get(key), live_global[key], banned_scan)
        if isinstance(decision, DeclinedChange):
            declined.append(decision)
        elif isinstance(decision, SnapshotChange):
            changes.append(decision)
            emitted[key] = decision.proposed

    return SnapshotPlan(
        toml=render_toml(emitted, base_text=shipped.text),
        changes=tuple(changes),
        declined=tuple(declined),
        skipped_secret=tuple(reported.skipped_secret),
        skipped_personal=tuple(reported.skipped_personal),
        stale_keys=tuple(reported.stale_keys),
        overlay_scope_rows=tuple(sorted(overlay_scope_rows)),
        dropped_keys=tuple(sorted(set(table) - set(emitted))),
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


def render_toml(emitted: dict[str, SettingValue], *, base_text: str = "") -> str:
    """Render *emitted* into the canonical ``[teatree]`` TOML text — the ONE shipped-file emitter.

    With *base_text* the CURRENT file is parsed and only its ``[teatree]`` table is
    replaced, so every sibling table (the ``[loops]`` / ``[modes]`` / ``[schedules]`` seed
    defaults) and every hand-written comment survives a run byte-for-byte. Only when the
    file is absent is the whole document — header included — built from scratch.

    Both writers of the shipped shape call THIS function: the owner-approved snapshot and
    the defaults-shape ``config_setting export``. Two emitters would drift, and the
    byte-identical round trip would only catch the drift after it shipped.

    :func:`grouped_settings_table` nests the keys into the declaration hierarchy as real
    sub-tables, and reads back flat through
    :func:`~teatree.config.cold_defaults.flatten_settings_table`.
    """
    document = tomlkit.parse(base_text) if base_text else tomlkit.document()
    document["teatree"] = grouped_settings_table(emitted)
    return tomlkit.dumps(document) if base_text else _HEADER + "\n" + tomlkit.dumps(document)
