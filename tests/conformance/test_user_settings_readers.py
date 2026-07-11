"""UserSettings field ↔ ≥1 real reader — the dead-config totality lane (SELFCATCH-4).

``test_removed_dead_settings.py`` guards exactly two historically-removed fields
(``branch_prefix``, ``ask_before_post_on_behalf``, #2731) and ``test_feature_flags``
governs only flag lifecycle. Neither is total: a NEW ``UserSettings`` field that
nothing reads — the dead-toggle class — ships green, an operator-facing knob that
silently does nothing.

This lane makes the guard total by introspection — but a reader must be a REAL
settings read, not a coincidental token match. A bare ``django.utils.timezone``
attribute (receiver is a module, not a settings object) and a keyword string
``"privacy"`` in an unrelated intent catalog would each falsely "read" a field
that the codebase itself documents as reader-less, hollowing the lane out. So a
read is one of: a ``.<field>`` access whose RECEIVER resolves to a settings object
(``get_effective_settings()`` / ``load_config().user`` / a var bound to those / a
high-confidence ``settings``/``cfg``/``config`` receiver), a ``getattr(settings,
"<field>")`` / cold-reader key read, a ``<NAME>_KEY = "<field>"`` config-key
constant, a field-name string literal inside a settings-RESOLUTION module (where
such a string IS a config key — ``getattr(settings, key)`` over a dict/tuple of
key names, but NOT a name used only as a bare-dict ``.get("<key>")`` sub-key of
a bespoke structured table nor a dict-literal ``{"<key>": ...}`` key, both
coincidental collisions), or the field on a
non-comment line of a ``hooks/*.sh`` cold-read script. A field read by none is dead config, unless named in
``FIELDS_WITHOUT_SRC_READER`` — the reviewable allowlist of fields consumed by
agent-prose / documentation rather than ``src`` code, or documented reader-less.
"""

import ast
import dataclasses
from collections.abc import Callable
from pathlib import Path

from teatree.config import UserSettings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_DEF = (_REPO_ROOT / "src" / "teatree" / "config" / "settings.py").resolve()
_PY_ROOTS = (_REPO_ROOT / "src" / "teatree", _REPO_ROOT / "hooks")
_SH_ROOT = _REPO_ROOT / "hooks"

#: Functions returning a ``UserSettings`` — a call to one is a settings object.
_SETTINGS_ACCESSORS = frozenset({"get_effective_settings"})
#: Functions returning a ``TeaTreeConfig`` — its ``.user`` is a ``UserSettings``.
_CONFIG_ACCESSORS = frozenset({"load_config"})
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

# Fields whose ONLY consumer is not ``src`` Python — documented, reviewable.
# ``e2e_confidence_threshold`` is documentation-driven by design (BLUEPRINT §
# configuration: "the typed field is the shared source of truth for the doc value
# and any future programmatic consumer"); the ``/t3:e2e`` verify↔review loop is
# agent prose, not a deterministic gate, so it reads the value from the skill.
# ``issue_implementer_cadence_hours`` is the documented cadence default the
# issue-implementer loop mirrors as a literal (``default_cadence_seconds=3600``).
# ``privacy`` (settings.py: "no live production reader") and ``timezone``
# (settings.py: "no live reader", DB-home for partition consistency) are declared
# reader-less at the source. A NEW dead field is NOT on this list and fails until
# it gains a real reader or a conscious allowlist entry.
FIELDS_WITHOUT_SRC_READER: frozenset[str] = frozenset(
    {
        "e2e_confidence_threshold",
        "issue_implementer_cadence_hours",
        "privacy",
        "timezone",
    }
)


def _field_names() -> set[str]:
    return {field.name for field in dataclasses.fields(UserSettings)}


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
    """``node`` evaluates to a ``UserSettings`` — an accessor call, ``.user``, or a bound/known var."""
    if isinstance(node, ast.Call) and _callee_name(node.func) in _SETTINGS_ACCESSORS:
        return True
    if isinstance(node, ast.Attribute) and node.attr == "user" and _is_config_expr(node.value, config_vars):
        return True
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
    (e.g. the ``availability_schedule`` dict's ``"timezone"``) collides with a
    UserSettings field name but is NOT a settings-key read. The receiver is a
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
    ``LoopSchedule`` model attribute — never a settings object) collides with a
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


def _file_python_readers(tree: ast.Module, field_names: set[str]) -> set[str]:
    read: set[str] = set()
    settings_vars, config_vars = _settings_vars_in(tree)
    resolves = _module_resolves_settings(tree)
    coincidental_keys = _bespoke_dict_get_keys(tree, settings_vars) | _dict_literal_keys(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in field_names
            and _is_settings_expr(node.value, settings_vars, config_vars)
        ):
            read.add(node.attr)
        elif isinstance(node, ast.Call) and _callee_name(node.func) in _READ_HELPERS:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value in field_names:
                    read.add(arg.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.endswith("_KEY")
                    and isinstance(node.value, ast.Constant)
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


def _walk_py_files(reader: Callable[[ast.Module, set[str]], set[str]], field_names: set[str]) -> set[str]:
    read: set[str] = set()
    for root in _PY_ROOTS:
        for path in root.rglob("*.py"):
            if path.resolve() == _SETTINGS_DEF:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
            except SyntaxError:
                continue
            read |= reader(tree, field_names)
    return read


def _python_readers(field_names: set[str]) -> set[str]:
    """Fields read by a REAL settings read across ``src``/``hooks`` Python.

    Introspection, not a hand-list; AST-based so a comment or prose docstring
    naming a field never counts. A read is a settings-object attribute access, a
    ``getattr``/cold-reader key literal, a ``*_KEY`` config-key constant, or a
    field-name string inside a settings-resolution module (where it is a key).
    """
    return _walk_py_files(_file_python_readers, field_names)


def _shell_readers(field_names: set[str]) -> set[str]:
    """Fields named on a non-comment line of a ``hooks/*.sh`` script (the cold-read seam).

    ``statusline_chain`` is read by ``statusline.sh`` via a direct ``sqlite3`` query
    on the ConfigSetting store before Django is up — a real reader a Python walk
    cannot see. Comment lines (``#``-led) are skipped.
    """
    read: set[str] = set()
    for path in _SH_ROOT.rglob("*.sh"):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for name in field_names:
                if name in line:
                    read.add(name)
    return read


def _all_readers(field_names: set[str]) -> set[str]:
    return _python_readers(field_names) | _shell_readers(field_names)


def unread_fields() -> set[str]:
    """Every ``UserSettings`` field with no real reader and no allowlist entry — the lane core."""
    names = _field_names()
    return names - _all_readers(names) - FIELDS_WITHOUT_SRC_READER


class TestEveryUserSettingsFieldIsRead:
    """A setting nothing reads is dead config — every field has a real consumer."""

    def test_no_user_settings_field_is_dead_config(self) -> None:
        unread = sorted(unread_fields())
        assert not unread, (
            "UserSettings field(s) that no src/hooks consumer reads (dead config) — "
            "wire a reader or add to FIELDS_WITHOUT_SRC_READER on purpose: " + str(unread)
        )

    def test_allowlisted_fields_are_still_real_fields(self) -> None:
        # A stale allowlist entry (a field that was removed) is dead surface.
        stale = sorted(FIELDS_WITHOUT_SRC_READER - _field_names())
        assert not stale, f"FIELDS_WITHOUT_SRC_READER entries that are not UserSettings fields: {stale}"

    def test_allowlisted_fields_genuinely_lack_a_src_reader(self) -> None:
        # The teeth of the tightening: each allowlisted field must be UNfound by the
        # REAL-reader matcher (the loose matcher counted privacy/timezone on a
        # coincidental token). An allowlisted field that now has a real reader must
        # be dropped from the allowlist, not left masking coverage.
        redundant = sorted(FIELDS_WITHOUT_SRC_READER & _all_readers(_field_names()))
        assert not redundant, f"FIELDS_WITHOUT_SRC_READER entries that now HAVE a real reader (drop them): {redundant}"


class TestUserSettingsReadersCardinalityFloors:
    """Anti-vacuity — a broken reader walk that finds nothing must not pass green."""

    def test_field_and_reader_floors(self) -> None:
        names = _field_names()
        read = _all_readers(names)
        assert len(names) >= 100, len(names)
        # The overwhelming majority of fields must resolve a REAL reader, else the walk broke.
        assert len(read) >= 100, len(read)


class TestUserSettingsReadersFiresRed:
    """Anti-vacuity — a synthetic never-read field is caught, and the tightening has teeth."""

    def test_a_synthetic_unread_field_is_named(self) -> None:
        # A field name no reader mentions and no allowlist names is exactly the
        # dead-toggle class the lane must flag.
        names = _field_names() | {"synthetic_dead_toggle_nothing_reads"}
        unread = names - _all_readers(names) - FIELDS_WITHOUT_SRC_READER
        assert "synthetic_dead_toggle_nothing_reads" in unread

    def test_tightening_catches_a_coincidentally_matched_dead_field(self) -> None:
        # THE fix: a dead field whose common name collides with an unrelated token
        # (``.payload`` on a ScanSignal, never a settings-object attribute) is
        # counted by a LOOSE matcher but caught by the REAL-reader matcher.
        loose = _loose_readers(_field_names() | {"payload"})
        tight = _all_readers(_field_names() | {"payload"})
        assert "payload" in loose, "expected the loose matcher to falsely count the coincidental token"
        assert "payload" not in tight, "the tightened matcher must NOT count a coincidental non-settings token"

    def test_documented_reader_less_fields_are_only_green_via_the_allowlist(self) -> None:
        # privacy/timezone are the real-world coincidence cases the loose matcher
        # passed; the tightened matcher flags them, so ONLY the allowlist keeps them
        # green — proof the lane would catch them if the allowlist were dropped.
        tight = _all_readers(_field_names())
        assert "privacy" not in tight
        assert "timezone" not in tight

    def test_a_real_read_field_is_not_flagged(self) -> None:
        # Positive control: a field with a real ``settings.<field>`` reader resolves.
        names = _field_names()
        assert "max_concurrent_local_stacks" in names
        assert "max_concurrent_local_stacks" in _all_readers(names)


def _loose_readers(field_names: set[str]) -> set[str]:
    """The OLD loose matcher (any ``.<field>`` attr OR exact ``"<field>"`` string).

    Retained ONLY to prove — in ``TestUserSettingsReadersFiresRed`` — that the
    tightened matcher rejects the coincidental token matches the loose one accepted.
    """

    def _loose_file(tree: ast.Module, names: set[str]) -> set[str]:
        read: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in names:
                read.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
                read.add(node.value)
        return read

    return _walk_py_files(_loose_file, field_names)
