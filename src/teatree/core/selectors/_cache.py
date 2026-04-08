import time
from collections.abc import Callable
from typing import cast

_panel_cache: dict[str, tuple[float, object]] = {}
_DEFAULT_PANEL_TTL = 5.0  # seconds
_SESSIONS_PANEL_TTL = 3.0  # shorter for filesystem I/O heavy panel


def _cached[T](key: str, builder: Callable[[], T], *, ttl: float = _DEFAULT_PANEL_TTL) -> T:
    """Return cached panel result if within TTL, otherwise rebuild."""
    now = time.monotonic()
    entry = _panel_cache.get(key)
    if entry is not None:
        cached_at, value = entry
        if now - cached_at < ttl:
            return cast("T", value)
    value = builder()
    _panel_cache[key] = (now, value)
    return value


def invalidate_panel_cache(panel: str | None = None) -> None:
    """Clear cached panel data. If *panel* is ``None``, clear everything."""
    if panel is None:
        _panel_cache.clear()
    else:
        _panel_cache.pop(panel, None)
