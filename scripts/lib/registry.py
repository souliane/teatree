"""Extension point registry with 3-layer priority: default < framework < project."""

from collections.abc import Callable
from typing import Any

_LAYERS = ("default", "framework", "project")
_LAYER_RANK = {layer: i for i, layer in enumerate(_LAYERS)}

# {point_name: [(layer, callable), ...]}  — kept sorted by layer rank
_registry: dict[str, list[tuple[str, Callable]]] = {}


def register(point: str, fn: Callable, layer: str = "default") -> None:
    if layer not in _LAYER_RANK:
        msg = f"Unknown layer {layer!r}. Must be one of {_LAYERS}"
        raise ValueError(msg)
    entries = _registry.setdefault(point, [])
    # Replace existing entry at same layer if present
    entries[:] = [(lyr, func) for lyr, func in entries if lyr != layer]
    entries.append((layer, fn))
    entries.sort(key=lambda x: _LAYER_RANK[x[0]])


def get(point: str) -> Callable | None:
    entries = _registry.get(point)
    if not entries:
        return None
    # Highest priority = last entry (highest layer rank)
    return entries[-1][1]


def call(point: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
    fn = get(point)
    if fn is None:
        msg = f"No handler registered for extension point {point!r}"
        raise KeyError(msg)
    return fn(*args, **kwargs)


def registered_points() -> list[str]:
    return list(_registry.keys())


def clear() -> None:
    _registry.clear()
