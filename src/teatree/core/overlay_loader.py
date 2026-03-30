"""Discover and cache overlay instances from ``teatree.overlays`` entry points."""

from functools import lru_cache
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def get_overlay(name: str | None = None) -> "OverlayBase":
    overlays = _discover_overlays()
    if not overlays:
        msg = "No teatree overlays found. Install a package that provides a 'teatree.overlays' entry point."
        raise ImproperlyConfigured(msg)

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


@lru_cache(maxsize=1)
def _discover_overlays() -> "dict[str, OverlayBase]":
    import importlib.metadata  # noqa: PLC0415

    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    eps = importlib.metadata.entry_points(group="teatree.overlays")
    result: dict[str, OverlayBase] = {}
    for ep in eps:
        cls = ep.load()
        if not issubclass(cls, OverlayBase):
            msg = f"Entry point {ep.name!r} ({ep.value}) does not subclass OverlayBase"
            raise ImproperlyConfigured(msg)
        result[ep.name] = cls()
    return result


def reset_overlay_cache() -> None:
    _discover_overlays.cache_clear()
