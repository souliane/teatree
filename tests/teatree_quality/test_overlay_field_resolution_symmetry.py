# test-path: cross-cutting — architecture fitness function over overlay config-field resolution.
"""Fitness test: per-overlay config-field resolution is path-only-symmetric.

Every overlay config field that the codebase resolves *per overlay* for an
INSTANTIABLE overlay (an entry-point package or a ``class``-carrying TOML
table) must ALSO resolve for a PATH-ONLY overlay (a ``path`` but no Python
``class``) — or be explicitly rejected at load. A path-only overlay is skipped
by ``get_all_overlays()``, so any field consumed through that dict alone is
invisible for it. A scope/visibility field that is invisible for a path-only
overlay silently breaks the gate that consumes it (e.g. the fail-CLOSED
owned-repo gate cannot see a path-only overlay's ``owned_repos``).

The single source of truth is ``OverlayConfigResolver.RESOLVABLE_FIELDS`` —
the registry of ``field-name -> resolver``. This test asserts each registered
resolver answers a path-only overlay's TOML-declared value from the raw
``[overlays.<name>]`` table, exactly as it answers an instantiable overlay
from its ``config``. The anti-vacuity: before ``owned_repos`` has a resolver,
``owned_repos`` is missing from the registry and this test goes RED on the
asymmetry assertion below.
"""

from contextlib import ExitStack
from unittest.mock import patch

import teatree.config as config_mod
from teatree.config import TeaTreeConfig
from teatree.core.overlay_loader import OverlayConfigResolver

# Fields that gate SCOPE (owned vs unknown) or VISIBILITY decisions per overlay.
# Every one of these MUST have a path-only-symmetric resolver, because a
# path-only overlay legitimately declares them in its TOML table and the gates
# that read them fail-CLOSED (scope) or scan-as-public (visibility) when blind.
_SCOPE_AND_VISIBILITY_FIELDS = {"frontend_repos", "owned_repos"}


def _make_config(overlays: dict) -> TeaTreeConfig:
    return TeaTreeConfig(raw={"overlays": overlays})


def _patch_landscape(overlays: dict, discovered: dict | None) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(config_mod, "load_config", return_value=_make_config(overlays)))
    stack.enter_context(patch("teatree.core.overlay_loader._discover_overlays", return_value=discovered or {}))
    return stack


def test_every_scope_and_visibility_field_has_a_path_only_resolver() -> None:
    """The asymmetry guard: no scope/visibility field may lack a path-only resolver."""
    registered = set(OverlayConfigResolver.RESOLVABLE_FIELDS)
    missing = _SCOPE_AND_VISIBILITY_FIELDS - registered
    assert not missing, (
        f"scope/visibility field(s) {sorted(missing)} have no per-overlay resolver; "
        "a path-only overlay's value for them is invisible to the gate that consumes it"
    )


def test_each_resolver_answers_a_path_only_overlay_from_its_toml_table() -> None:
    """Each registered resolver reads a path-only overlay's declared value, never raises."""
    declared = {
        "frontend_repos": ["acme-web", "acme-admin"],
        "owned_repos": {"github.com": ["acme-eng"]},
    }
    for field, resolver in OverlayConfigResolver.RESOLVABLE_FIELDS.items():
        value = declared[field]
        overlays = {"t3-path": {"path": "~/x/t3-path", field: value}}
        with _patch_landscape(overlays, discovered={}):
            assert resolver("t3-path") == value, f"path-only resolver for {field!r} did not read its TOML value"


def test_each_resolver_defaults_empty_for_a_path_only_overlay_without_the_field() -> None:
    """A path-only overlay that omits the field resolves to the field's empty default, not a raise."""
    empties = {"frontend_repos": [], "owned_repos": {}}
    for field, resolver in OverlayConfigResolver.RESOLVABLE_FIELDS.items():
        overlays = {"t3-path": {"path": "~/x/t3-path", "protected_branches": ["development"]}}
        with _patch_landscape(overlays, discovered={}):
            assert resolver("t3-path") == empties[field], f"path-only resolver for {field!r} did not default empty"
