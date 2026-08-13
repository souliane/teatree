"""Every key-string config key ↔ ≥1 real reader — the dead-config lane for the non-``UserSettings`` partitions (#4203).

``test_user_settings_readers`` makes the guard total for the ``UserSettings``
partition. The other three registries — ``COLD_SETTINGS`` / ``REGISTRY_SETTINGS``
(``config.registries``) and ``COLD_HOOK_SETTINGS`` — had no such lane, so a key
declared there and read by nobody shipped green: ``availability_schedule`` sat in
``REGISTRY_SETTINGS`` with zero readers across the whole tree while the operator-facing
availability surface it configured had already been cut in favour of presets.

These keys are resolved by KEY STRING through ``cold_reader`` rather than as a
``UserSettings`` attribute, so the reader notion here is the matching one and is
deliberately GENEROUS: a key counts as read on any non-docstring string literal equal
to it, any ``.<key>`` attribute access, or any non-comment line of a cold-read
``hooks/*.sh`` script. Generosity only ever UNDER-reports dead keys, so this lane
cannot produce a false RED — a key it flags has no textual reader at all.
"""

import ast
import re
import sys
from functools import cache
from pathlib import Path

import pytest

from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.registries import COLD_SETTINGS, REGISTRY_SETTINGS
from tests.conformance._src_tree import REPO_ROOT, SRC_DIR, parsed_modules

_HOOKS_DIR = REPO_ROOT / "hooks"
_PY_ROOTS = (SRC_DIR, _HOOKS_DIR)

#: Modules that DECLARE config keys rather than read them. A key's presence in one is
#: what makes it exist at all, so counting these would make every key self-reading.
_DECLARATION_MODULES: frozenset[Path] = frozenset(
    (SRC_DIR / "config" / name).resolve()
    for name in (
        "schema.py",
        "settings.py",
        "settings_loop_flags.py",
        "setting_help.py",
        "setting_registries.py",
        "setting_groups.py",
        "registries.py",
        "cold_hook_settings.py",
        "secret_settings.py",
        "known_settings.py",
        "overlay_code_defaults.py",
        "defaults_approvals.py",
        "host_projection.py",
        "retired_settings.py",
    )
)


@cache
def _key_names() -> frozenset[str]:
    """Every config key resolved by key string — the three non-``UserSettings`` registries."""
    return frozenset({*COLD_SETTINGS, *REGISTRY_SETTINGS, *COLD_HOOK_SETTINGS})


def _docstring_nodes(tree: ast.Module) -> frozenset[int]:
    return frozenset(
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    )


def _module_readers(tree: ast.Module, keys: frozenset[str]) -> set[str]:
    docstrings = _docstring_nodes(tree)
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in keys:
            read.add(node.attr)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and node.value in keys
        ):
            read.add(node.value)
    return read


@cache
def _keys_with_a_reader() -> frozenset[str]:
    keys = _key_names()
    read: set[str] = set()
    for root in _PY_ROOTS:
        for path, tree in parsed_modules(root):
            # Migrations are FROZEN ORM state, never live readers — the same exclusion
            # the UserSettings lane makes, for the same reason.
            if path.resolve() in _DECLARATION_MODULES or "migrations" in path.parts:
                continue
            read |= _module_readers(tree, keys)
    for path in sorted(_HOOKS_DIR.rglob("*.sh")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("#"):
                continue
            read |= {key for key in keys if re.search(rf"\b{re.escape(key)}\b", line)}
    return frozenset(read)


def test_every_key_string_config_key_has_a_reader() -> None:
    """A registry key nothing resolves is an operator-facing knob that does nothing."""
    dead = sorted(_key_names() - _keys_with_a_reader())
    assert dead == [], (
        f"config keys with no reader anywhere in src/ or hooks/: {dead}. "
        "Either wire the reader or retire the key through teatree.config.retired_settings."
    )


def test_the_scan_would_catch_a_planted_dead_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REAL control.

    ``_keys_with_a_reader()`` is always a SUBSET of ``_key_names()`` by construction, so
    asserting a name outside the registry stays unread (the old control) is vacuous — it
    would pass even if the scan body were replaced with ``return _key_names()``, a lane
    that can never report anything dead. This plants a synthetic key INSIDE the scanned
    set itself (exactly where a genuine dead key would sit) and asserts the scan still
    reports it dead, so a broken scan that marks everything read fails HERE.
    """
    planted = "zz_config_key_readers_control_4203_never_referenced_anywhere"
    live = _key_names()
    assert planted not in live  # sanity: the plant is not already a real key
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_key_names", lambda: frozenset({*live, planted}))
    _keys_with_a_reader.cache_clear()
    try:
        assert planted not in _keys_with_a_reader()
    finally:
        _keys_with_a_reader.cache_clear()
