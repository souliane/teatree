import os
import time
from collections.abc import Callable
from typing import cast

_panel_cache: dict[str, tuple[float, object]] = {}
# Override via ``TEATREE_PANEL_CACHE_TTL`` (0 disables caching — used by E2E tests).
_DEFAULT_PANEL_TTL = float(os.environ.get("TEATREE_PANEL_CACHE_TTL", "5.0"))
_SESSIONS_PANEL_TTL = min(_DEFAULT_PANEL_TTL, 3.0) if _DEFAULT_PANEL_TTL > 0 else 0.0


def _cached[T](key: str, builder: Callable[[], T], *, ttl: float = _DEFAULT_PANEL_TTL) -> T:
    """Return cached panel result if within TTL, otherwise rebuild."""
    if ttl <= 0:
        return builder()
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
