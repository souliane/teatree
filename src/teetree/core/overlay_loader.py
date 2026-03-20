from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from teetree.core.overlay import OverlayBase


def get_overlay() -> OverlayBase:
    return _load_overlay()


@lru_cache(maxsize=1)
def _load_overlay() -> OverlayBase:
    overlay_path = getattr(settings, "TEATREE_OVERLAY_CLASS", "")
    if not overlay_path:
        msg = "TEATREE_OVERLAY_CLASS must be configured"
        raise ImproperlyConfigured(msg)

    try:
        overlay_class = import_string(overlay_path)
    except ImportError as exc:
        msg = f"Could not import overlay: {overlay_path}"
        raise ImproperlyConfigured(msg) from exc

    if not issubclass(overlay_class, OverlayBase):
        msg = "Configured overlay must subclass OverlayBase"
        raise ImproperlyConfigured(msg)

    return overlay_class()


def reset_overlay_cache() -> None:
    _load_overlay.cache_clear()
