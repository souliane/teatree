"""Which settings NOBODY reads — the totality half of the ownership question.

:mod:`teatree.quality.loop_setting_ownership` answers "which loop owns this key" for the
keys a loop owns. It says nothing about the rest, so the question the surface-size problem
actually turns on — *which of the declared settings does nobody own?* — stayed
unanswerable, and an exposed surface nobody can account for is how a registry grows past
what anyone maintains.

Ownership is TOTAL by construction: every key that does anything has a reader. So the
assertion this module exists for is one-sided and cheap — the UNREAD set is empty, or
every member of it carries a recorded reason.

**Its reach is the thing to get right, and the naive reach was wrong in three ways**,
each measured against a key the narrow walk called unread while a reader plainly existed:

*   ``hooks/`` sits outside the package, and ``loop_cadence_seconds`` is read only there;
*   a ``.sh`` reader has no Python AST at all — ``statusline_engaged_render`` is read by
    ``hooks/scripts/statusline.sh`` through the sqlite CLI and by nothing else;
*   ``config/`` is not uniformly a declaration. Its RESOLVERS are readers, and excluding
    the package wholesale hid the only read of
    ``independent_reviewer_identities``.

A miss in any of those directions produces a FALSE ABSENCE — a key reported as owned by
nobody while its owner is one directory over — which is worse than no answer, because it
invites a deletion. So the walk fails LOUD on a root it cannot read rather than treating
an unreadable tree as one holding no readers.
"""

import ast
import re
from collections.abc import Collection, Iterator
from pathlib import Path

#: Directories under ``config/`` that DECLARE rather than read: a schema field, a help
#: string and a default are the key's definition, so counting them as readership would
#: make every key trivially read and the assertion vacuous. Everything else in
#: ``config/`` — the resolvers, the registries that consult a value — is a reader.
DECLARATION_MODULES: frozenset[str] = frozenset(
    {
        "defaults_snapshot.py",
        "schema.py",
        "setting_groups.py",
        "setting_help.py",
        "setting_registries.py",
        "settings.py",
        "settings_loop_owned.py",
    }
)

#: A frozen record of a past schema, never a live consumer.
_FROZEN = ("core", "migrations")

#: Shell readers reach a setting through the sqlite CLI, so the key appears as a bare
#: literal. Matched on a word boundary, which is all a shell script offers.
_SHELL_SUFFIXES = (".sh",)


class UnreadableRootError(RuntimeError):
    """A root the readership walk could not read, so its answer would be a false absence."""

    def __init__(self, root: Path) -> None:
        super().__init__(f"readership root {root} is not a readable directory — refusing to report an UNREAD set")


def _sources(root: Path) -> Iterator[Path]:
    return (path for path in sorted(root.rglob("*")) if path.is_file() and "__pycache__" not in path.parts)


def _is_declaration(path: Path, package_root: Path) -> bool:
    return path.parent == package_root / "config" and path.name in DECLARATION_MODULES


def _is_frozen(path: Path, package_root: Path) -> bool:
    return path.is_relative_to(package_root.joinpath(*_FROZEN))


# ── What counts as a settings READ ──────────────────────────────────────────────
#
# The matcher below is shared with `tests/conformance/test_user_settings_readers.py`,
# which asserts the same property over `UserSettings` fields. It lived there, and this
# module carried a second, LOOSER copy — any `.<name>` attribute access or any bare
# `"<name>"` string — so a `django.utils.timezone` attribute and a keyword in an unrelated
# catalog both read as owners, and the unread set this module reports came back empty
# whether or not the keys had readers. The conformance lane keeps that loose matcher, but
# only as its negative control: the thing the tight one must reject.
#
# A read is one of: a `.<field>` access whose RECEIVER resolves to a settings object, a
# `getattr(settings, "<field>")` / cold-reader key read, a `<NAME>_KEY = "<field>"`
# constant, or a field-name string inside a settings-RESOLUTION module — where such a
# string IS a config key, but NOT when it is a bare-dict sub-key, a dict-literal key, or a
# Django admin option, which are three ways a name collides coincidentally.

#: Functions returning a ``UserSettings`` — a call to one is a settings object.
_SETTINGS_ACCESSORS = frozenset({"get_effective_settings"})
#: Functions returning a ``TeaTreeConfig`` whose ``.user`` is the EFFECTIVE settings —
#: empty, and load-bearing so. ``load_config().user`` is the dataclass defaults, as that
#: function's own docstring says, so counting it as a read made every defaults-only
#: consumer look wired: a stored value reached neither the resource-pressure scanner nor
#: `t3 setup`'s skill exclusions, and this lane said both were read. Nothing in ``src`` or
#: ``hooks`` reads a field that way any more, so the empty set is a ratchet, not a gap.
_CONFIG_ACCESSORS: frozenset[str] = frozenset()
#: High-confidence bare receiver names that hold a ``UserSettings`` object. A
#: lower-confidence receiver (``s``, ``user``) is only trusted when var-tracking
#: bound it to an accessor in the same file — never blanket-listed.
_SETTINGS_RECEIVERS = frozenset({"settings", "settings_", "cfg", "config", "user_settings", "effective_settings"})
#: Calls whose string-literal argument is a settings/config read KEY.
_READ_HELPERS = frozenset(
    {
        "getattr",
        "bool_setting",
        "int_setting",
        "str_setting",
        "value_setting",
        "read_setting",
        "_cold_db_bool",
        "_cold_db_int",
        "_cold_db_raw",
        "_cold_db_str",
    }
)
#: A module referencing any of these RESOLVES settings by key, so a field-name
#: string literal in it IS a config key (``getattr(settings, key)`` over a
#: dict/tuple of key names). An unrelated module (an intent catalog) has none, so
#: a keyword string that merely collides with a field name never counts there.
_RESOLUTION_MARKERS = frozenset(
    {
        "get_effective_settings",
        "load_config",
        "cold_reader",
        "read_setting",
        "_db_overlay_overrides",
        "_db_global_overrides",
        "ConfigSetting",
        "bool_setting",
        "int_setting",
        "str_setting",
        "value_setting",
    }
)


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_config_expr(node: ast.expr, config_vars: set[str]) -> bool:
    """``node`` evaluates to a ``TeaTreeConfig`` (its ``.user`` is the settings)."""
    if isinstance(node, ast.Call) and _callee_name(node.func) in _CONFIG_ACCESSORS:
        return True
    return isinstance(node, ast.Name) and node.id in config_vars


def _is_settings_expr(node: ast.expr, settings_vars: set[str], config_vars: set[str]) -> bool:
    """``node`` evaluates to a ``UserSettings`` — an accessor call, ``.user``, a var, or an injectable default.

    The injectable-default idiom binds an optional caller-supplied settings object
    to the live accessor: ``settings if settings is not None else
    get_effective_settings(...)`` (ternary) or ``settings or
    get_effective_settings(...)`` (boolean). Both evaluate to a ``UserSettings``,
    and EVERY branch must itself resolve to one, so recursing here cannot admit a
    non-settings receiver — it only stops a real reader behind a defaulted
    parameter from reading as dead config.
    """
    if isinstance(node, ast.Call) and _callee_name(node.func) in _SETTINGS_ACCESSORS:
        return True
    if isinstance(node, ast.Attribute) and node.attr == "user" and _is_config_expr(node.value, config_vars):
        return True
    if isinstance(node, ast.IfExp):
        return all(_is_settings_expr(branch, settings_vars, config_vars) for branch in (node.body, node.orelse))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return all(_is_settings_expr(value, settings_vars, config_vars) for value in node.values)
    return isinstance(node, ast.Name) and (node.id in settings_vars or node.id in _SETTINGS_RECEIVERS)


def _module_resolves_settings(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _RESOLUTION_MARKERS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _RESOLUTION_MARKERS:
            return True
    return False


def _settings_vars_in(tree: ast.Module) -> tuple[set[str], set[str]]:
    settings_vars: set[str] = set()
    config_vars: set[str] = set()
    for _ in range(3):  # fixpoint over settings-var assignment propagation
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if _is_settings_expr(node.value, settings_vars, config_vars):
                    settings_vars.add(target.id)
                elif _is_config_expr(node.value, config_vars):
                    config_vars.add(target.id)
    return settings_vars, config_vars


def _bespoke_dict_get_keys(tree: ast.Module, settings_vars: set[str]) -> set[str]:
    """String keys read via ``<bare-var>.get("<key>")`` off a plain dict.

    A field name that appears ONLY as a sub-key of a bespoke structured table
    collides with a UserSettings field name but is NOT a settings-key read. The receiver is a
    bare local (a table var like ``raw``), never a settings-store accessor
    call (``_db_overlay_overrides(...).get("workspace_dir")`` keeps its Call
    receiver and stays counted), so the resolution-module string rule must not
    count it as a reader.
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
            continue
        receiver = node.func.value
        if not isinstance(receiver, ast.Name) or receiver.id in settings_vars or receiver.id in _SETTINGS_RECEIVERS:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            keys.add(node.args[0].value)
    return keys


def _dict_literal_keys(tree: ast.Module) -> set[str]:
    """String keys of dict literals ``{"<key>": ...}`` — structural keys, not settings reads.

    A field name that appears only as a dict-literal key (e.g. the schedule
    command's ``{"timezone": schedule.timezone}`` output row, whose value is a
    ``ModeSchedule`` model attribute — never a settings object) collides with a
    UserSettings field name but is NOT a settings-key read. It is the same
    coincidental class as the ``<bare-var>.get("<key>")`` sub-key rule above, in
    dict-literal form, so the resolution-module string rule must not count it.
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


# Django ``ModelAdmin`` / inline option attributes whose value is a tuple/list of
# MODEL field (or method) names — structural column references, never settings reads.
_ADMIN_OPTION_ATTRS = frozenset(
    {
        "list_display",
        "list_display_links",
        "list_editable",
        "list_filter",
        "list_select_related",
        "search_fields",
        "readonly_fields",
        "fields",
        "exclude",
        "ordering",
        "raw_id_fields",
        "autocomplete_fields",
        "filter_horizontal",
        "filter_vertical",
        "sortable_by",
    }
)


def _admin_option_strings(tree: ast.Module) -> set[str]:
    """String elements of Django admin option tuples (``list_display = ("timezone", …)``).

    A field name listed in a ``ModelAdmin``'s ``list_display`` / ``fields`` / … is a
    MODEL column reference (e.g. ``ModeSchedule.timezone``), never a settings-object
    read — the same coincidental class as :func:`_dict_literal_keys`, in admin-option
    form. Excluded so a bare field-name string in an admin option never counts as a
    settings reader.
    """
    strings: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Tuple | ast.List):
            continue
        if not any(isinstance(target, ast.Name) and target.id in _ADMIN_OPTION_ATTRS for target in node.targets):
            continue
        strings.update(
            elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
    return strings


def file_settings_reads(tree: ast.Module, field_names: set[str]) -> set[str]:
    """Every key *tree* genuinely reads off a settings object — the ONE matcher both lanes use."""
    read: set[str] = set()
    settings_vars, config_vars = _settings_vars_in(tree)
    resolves = _module_resolves_settings(tree)
    coincidental_keys = (
        _bespoke_dict_get_keys(tree, settings_vars) | _dict_literal_keys(tree) | _admin_option_strings(tree)
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in field_names
            and _is_settings_expr(node.value, settings_vars, config_vars)
        ):
            read.add(node.attr)
        elif isinstance(node, ast.Call) and _callee_name(node.func) in _READ_HELPERS:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in field_names:
                    read.add(arg.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.endswith("_KEY")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and node.value.value in field_names
                ):
                    read.add(node.value.value)
        elif (
            resolves
            and isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in field_names
            and node.value not in coincidental_keys
        ):
            read.add(node.value)
    return read


def _python_reads(path: Path, keys: frozenset[str]) -> set[str]:
    """Keys *path* genuinely reads off a settings object.

    A file this walk cannot parse contributes nothing, which is the one direction that
    cannot manufacture a reader — an unreadable ROOT still raises, since that is where a
    silent empty would read as "nobody owns these".
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    return file_settings_reads(tree, set(keys))


def _shell_reads(path: Path, keys: frozenset[str]) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {key for key in keys if re.search(rf"\b{re.escape(key)}\b", text)}


def read_sites(roots: Collection[Path], keys: Collection[str], *, package_root: Path) -> dict[str, set[Path]]:
    """Every file that READS each key, across *roots*.

    Raises :class:`UnreadableRootError` for a root that is not a readable directory: an
    empty answer from a tree nobody could open is indistinguishable from a tree with no
    readers, and only one of those licenses a deletion.
    """
    wanted = frozenset(keys)
    sites: dict[str, set[Path]] = {key: set() for key in wanted}
    for root in roots:
        if not root.is_dir():
            raise UnreadableRootError(root)
        for path in _sources(root):
            if _is_declaration(path, package_root) or _is_frozen(path, package_root):
                continue
            if path.suffix == ".py":
                found = _python_reads(path, wanted)
            elif path.suffix in _SHELL_SUFFIXES:
                found = _shell_reads(path, wanted)
            else:
                continue
            for key in found:
                sites[key].add(path)
    return sites


def unread_settings(roots: Collection[Path], keys: Collection[str], *, package_root: Path) -> tuple[str, ...]:
    """The declared keys no file in *roots* reads — the ones nobody owns, sorted."""
    sites = read_sites(roots, keys, package_root=package_root)
    return tuple(sorted(key for key, found in sites.items() if not found))


__all__ = ["DECLARATION_MODULES", "UnreadableRootError", "file_settings_reads", "read_sites", "unread_settings"]
