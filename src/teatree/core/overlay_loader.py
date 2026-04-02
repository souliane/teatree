"""Discover and cache overlay instances from entry points and TOML config.

Unifies both discovery mechanisms so that ``get_overlay()`` works regardless
of whether the overlay was registered via ``pip install`` (entry point) or
``~/.teatree.toml`` (TOML config).
"""

import importlib
import logging
import os
from functools import lru_cache
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


def get_overlay(name: str | None = None) -> "OverlayBase":
    overlays = _discover_overlays()
    if not overlays:
        msg = (
            "No teatree overlays found. Install a package that provides a"
            " 'teatree.overlays' entry point, or add one to ~/.teatree.toml."
        )
        raise ImproperlyConfigured(msg)

    if name is None:
        name = os.environ.get("T3_OVERLAY_NAME") or None

    if name is not None:
        try:
            return overlays[name]
        except KeyError:
            msg = f"Overlay {name!r} not found. Available: {', '.join(sorted(overlays))}"
            raise ImproperlyConfigured(msg) from None

    if len(overlays) == 1:
        return next(iter(overlays.values()))

    msg = f"Multiple overlays found ({', '.join(sorted(overlays))}). Pass an explicit name to get_overlay()."
    raise ImproperlyConfigured(msg)


def get_all_overlays() -> "dict[str, OverlayBase]":
    return dict(_discover_overlays())


def get_all_overlay_names() -> list[str]:
    """Return all overlay names, including path-only TOML entries.

    Unlike ``get_all_overlays()``, this includes TOML entries that declare a
    ``path`` but no ``class`` — they can't be instantiated as OverlayBase but
    should appear in the dashboard overlay selector for ticket filtering.
    """
    from teatree.config import load_config  # noqa: PLC0415

    names = set(_discover_overlays())
    config = load_config()
    for name, cfg in config.raw.get("overlays", {}).items():
        if cfg.get("path"):
            names.add(name)
    return sorted(names)


@lru_cache(maxsize=1)
def _discover_overlays() -> "dict[str, OverlayBase]":
    import importlib.metadata  # noqa: PLC0415

    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    result: dict[str, OverlayBase] = {}

    # 1. Entry-point overlays (pip-installed packages)
    eps = importlib.metadata.entry_points(group="teatree.overlays")
    for ep in eps:
        cls = ep.load()
        if not issubclass(cls, OverlayBase):
            msg = f"Entry point {ep.name!r} ({ep.value}) does not subclass OverlayBase"
            raise ImproperlyConfigured(msg)
        result[ep.name] = cls()

    # 2. TOML-configured overlays (not already found via entry points)
    result.update(_discover_toml_overlays(OverlayBase, set(result)))

    return result


def _discover_toml_overlays(
    base_class: type["OverlayBase"],
    already_found: set[str],
) -> "dict[str, OverlayBase]":
    """Discover overlays from ``~/.teatree.toml`` that aren't already entry-point-registered."""
    from teatree.config import load_config  # noqa: PLC0415

    result: dict[str, OverlayBase] = {}
    config = load_config()
    overlays_cfg = config.raw.get("overlays", {})

    for name, overlay_cfg in overlays_cfg.items():
        if name in already_found:
            continue

        class_path = overlay_cfg.get("class", "")
        if not class_path or ":" not in class_path:
            # No class path — this is a project-directory-only overlay without
            # a Python class.  These work through the CLI subprocess bridge
            # (OverlayAppBuilder) but can't be instantiated as OverlayBase.
            continue

        try:
            module_path, class_name = class_path.rsplit(":", 1)
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            if not issubclass(cls, base_class):
                logger.warning("TOML overlay %r class %s does not subclass OverlayBase", name, class_path)
                continue
            result[name] = cls()
        except (ImportError, AttributeError) as exc:
            logger.warning("TOML overlay %r failed to load class %s: %s", name, class_path, exc)

    return result


def reset_overlay_cache() -> None:
    _discover_overlays.cache_clear()
