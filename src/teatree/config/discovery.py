"""Overlay discovery — from ``~/.teatree.toml`` and installed entry points.

``discover_overlays`` / ``discover_active_overlay`` plus the entry-point /
manage.py resolution helpers. Split out of the package module for the
module-health LOC cap; re-exported from ``teatree.config``.
"""

import importlib.util
import os
from pathlib import Path
from typing import Any

import teatree.config as _facade
from teatree.config.settings import OVERLAY_OVERRIDABLE_SETTINGS, TOML_OVERLAY_OVERRIDABLE_SETTINGS, OverlayEntry


def discover_overlays(config_path: Path | None = None) -> list[OverlayEntry]:
    """Discover overlays from ~/.teatree.toml and installed entry points.

    Sources (merged by name, toml wins on conflict):
    1. ``[overlays.<name>]`` sections in the toml config (``path`` key)
    2. ``teatree.overlays`` entry-point group from installed packages

    A bare config-only ``[overlays.<alias>]`` table (no ``path``/``class``)
    whose name is a legacy short alias of an installed entry-point overlay
    is folded into that canonical entry-point overlay rather than emitted
    as a separate one — older ``slack-bot`` runs wrote ``[overlays.teatree]``
    for the ``t3-teatree`` overlay, which made discovery list both as if
    they were distinct overlays (souliane/teatree#1108).
    """
    from importlib.metadata import entry_points  # noqa: PLC0415

    if config_path is None:
        config_path = _facade.CONFIG_PATH
    seen: dict[str, OverlayEntry] = {}

    ep_names = {ep.name for ep in entry_points(group="teatree.overlays")}

    # 1. Toml config
    config = _facade.load_config(config_path)
    for name, overlay_cfg in config.raw.get("overlays", {}).items():
        overlay_class = overlay_cfg.get("class", "")
        path_str = overlay_cfg.get("path", "")
        project_path = Path(path_str).expanduser() if path_str else None
        overrides: dict[str, Any] = {}
        for key, parser in (OVERLAY_OVERRIDABLE_SETTINGS | TOML_OVERLAY_OVERRIDABLE_SETTINGS).items():
            if key in overlay_cfg:
                overrides[key] = parser(overlay_cfg[key])
        if not overlay_class and project_path is None and name not in ep_names:
            canonical = _match_canonical_ep(name, ep_names)
            if canonical is not None:
                # Legacy short-alias config table — fold its overrides into
                # the canonical entry-point overlay below; do not emit a
                # stray overlay under the alias name.
                continue
        if not overlay_class and project_path:
            manage_py = project_path / "manage.py"
            settings_module = _extract_settings_module(manage_py) if manage_py.is_file() else ""
            overlay_class = settings_module
        seen[name] = OverlayEntry(
            name=name,
            overlay_class=overlay_class,
            project_path=project_path,
            overrides=overrides,
        )

    # 2. Entry points (skip if already found via toml)
    for ep in entry_points(group="teatree.overlays"):
        if ep.name not in seen:
            seen[ep.name] = OverlayEntry(
                name=ep.name,
                overlay_class=ep.value,
                project_path=_resolve_ep_project_path(ep.value),
            )

    return list(seen.values())


def _match_canonical_ep(alias: str, ep_names: "set[str]") -> str | None:
    """Return the canonical overlay name a short ``alias`` maps to.

    Single home for the legacy-alias rule (souliane/teatree#1138): a bare
    ``[overlays.<alias>]`` table in ``~/.teatree.toml`` (without
    ``path``/``class``) maps to the installed overlay whose name equals
    ``alias`` or ends with ``"-<alias>"`` — e.g. a short
    ``[overlays.teatree]`` table folds into the canonical
    ``t3-teatree`` entry point.

    The dash separator in the suffix match is required: a name that
    happens to end with the alias *without* a dash (e.g. ``t3acme``
    for alias ``acme``) is a semantic collision, not a legacy alias,
    and is rejected. Returns ``None`` when no canonical match exists.
    """
    for ep_name in ep_names:
        if ep_name == alias or ep_name.endswith(f"-{alias}"):
            return ep_name
    return None


def discover_active_overlay() -> OverlayEntry | None:
    """Find the overlay to use.

    Priority:
    1. manage.py in cwd ancestors (developer workflow)
    2. Single installed overlay (end-user workflow)
    """
    local = _discover_from_manage_py()
    if local:
        return local

    installed = _facade.discover_overlays()
    if len(installed) == 1:
        return installed[0]

    return None


def _discover_from_manage_py() -> OverlayEntry | None:
    """Walk up from cwd to find a manage.py and extract its settings module.

    The directory basename names the overlay, but a clone dir can differ from
    the registered entry-point name (``teatree`` on disk vs the registered
    ``t3-teatree``). ``_canonical_active_overlay_name`` folds the basename onto
    the registered entry point so every consumer — most importantly the scanners
    that stamp ``ticket.overlay`` — writes the dispatchable name, never a stale
    alias that the queue then can't resolve (souliane/teatree#1959).
    """
    for directory in [Path.cwd(), *Path.cwd().parents]:
        manage_py = directory / "manage.py"
        if manage_py.is_file():
            settings_module = _extract_settings_module(manage_py)
            if settings_module:
                name = _canonical_active_overlay_name(directory.name)
                return OverlayEntry(name=name, overlay_class="", project_path=directory)
    return None


def _canonical_active_overlay_name(directory_name: str) -> str:
    """Fold a clone-directory basename onto its registered entry-point name, if one exists.

    Stays inside the ``platform`` layer (``config``): reads the entry-point
    names directly and reuses the local ``_match_canonical_ep`` alias rule
    rather than calling into the ``core`` overlay loader.
    """
    from importlib.metadata import entry_points  # noqa: PLC0415

    try:
        ep_names = {ep.name for ep in entry_points(group="teatree.overlays")}
    except Exception:  # noqa: BLE001 — discovery must not crash before django.setup()
        return directory_name
    if directory_name in ep_names:
        return directory_name
    return _match_canonical_ep(directory_name, ep_names) or directory_name


def _resolve_ep_project_path(overlay_class: str) -> Path | None:
    """Resolve the project root for an entry-point overlay from its class path.

    ``overlay_class`` is e.g. ``"teatree.contrib.t3_teatree.overlay:TeatreeOverlay"``.
    Parses the module part (before the ``:``) to find the top-level package on disk,
    then walks up to find a ``manage.py`` — the same marker used by TOML and cwd-based
    discovery.
    """
    module_path = overlay_class.split(":", maxsplit=1)[0]
    top_package = module_path.split(".", maxsplit=1)[0]
    spec = importlib.util.find_spec(top_package)
    if spec is None or not spec.submodule_search_locations:
        return None
    pkg_dir = Path(spec.submodule_search_locations[0])
    for parent in [pkg_dir, *pkg_dir.parents]:
        if (parent / "manage.py").is_file():
            return parent
    return None


def _extract_settings_module(manage_py: Path) -> str:
    text = manage_py.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "DJANGO_SETTINGS_MODULE" in line and '"' in line:
            return line.split('"')[-2]
    return ""


def _active_overlay_entry() -> OverlayEntry | None:
    """Find the active overlay's toml entry (carrying any overrides).

    Prefers ``T3_OVERLAY_NAME`` (the same env var ``get_overlay()`` uses)
    to avoid worktree-dir/overlay-name mismatch.
    """
    overlays = _facade.discover_overlays()
    by_name = {entry.name: entry for entry in overlays}

    name = os.environ.get("T3_OVERLAY_NAME")
    if name and name in by_name:
        return by_name[name]

    fallback = _facade.discover_active_overlay()
    if fallback is not None and fallback.name in by_name:
        # The cwd-based lookup returns a bare OverlayEntry without overrides;
        # swap in the toml entry so override parsing applies.
        return by_name[fallback.name]

    if len(overlays) == 1:
        return overlays[0]

    return None
