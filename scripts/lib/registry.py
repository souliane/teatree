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


def active_layer(point: str) -> str | None:
    entries = _registry.get(point)
    if not entries:
        return None
    return entries[-1][0]


def registered_points() -> list[str]:
    return list(_registry.keys())


def _handler_label(fn: Callable) -> str:
    mod = getattr(fn, "__module__", "?")
    name = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
    return f"{mod}.{name}"


def info() -> list[dict[str, Any]]:
    """Return full registry state: each EP with all registered layers and the active one."""
    result: list[dict[str, Any]] = []
    for point in sorted(_registry):
        entries = _registry[point]
        layers = {lyr: _handler_label(fn) for lyr, fn in entries}
        result.append(
            {
                "point": point,
                "active_layer": entries[-1][0],
                "active_handler": _handler_label(entries[-1][1]),
                "layers": layers,
            }
        )
    return result


class MissingOverrideError(RuntimeError):
    """Raised when a mandatory extension point has no project-layer override."""


# Extension points that MUST be overridden by a project overlay for the
# lifecycle workflow to function. The default handlers are no-ops.
_MANDATORY_FOR_SERVICES = ("wt_run_backend", "wt_run_frontend")
_MANDATORY_FOR_DB = ("wt_db_import", "wt_post_db")


def validate_overrides(phase: str = "services") -> None:
    """Verify that mandatory extension points have project-layer overrides.

    Raises MissingOverrideError with a detailed message if any mandatory EP
    is still at default/framework layer when it should be at project layer.

    Phases:
    - "services": checks wt_run_backend, wt_run_frontend
    - "db": checks wt_db_import, wt_post_db
    """
    required = {
        "services": _MANDATORY_FOR_SERVICES,
        "db": _MANDATORY_FOR_DB,
    }.get(phase, _MANDATORY_FOR_SERVICES)

    missing = []
    for ep in required:
        layer = active_layer(ep)
        if layer != "project":
            missing.append(f"  - {ep} (current layer: {layer or 'not registered'})")

    if missing:
        import os

        overlay = os.environ.get("T3_OVERLAY", "")
        pythonpath = os.environ.get("PYTHONPATH", "")
        msg = (
            "Mandatory extension points not overridden by project overlay:\n" + "\n".join(missing) + "\n\n"
            f"This usually means the project overlay's scripts/ directory is not on PYTHONPATH.\n"
            f"  T3_OVERLAY={overlay}\n"
            f"  PYTHONPATH={pythonpath}\n\n"
            f"Fix: ensure T3_OVERLAY is set in ~/.teatree and its scripts/ dir is on PYTHONPATH.\n"
            f'  export PYTHONPATH="$T3_REPO/scripts:$T3_OVERLAY/scripts"'
        )
        raise MissingOverrideError(msg)


def clear() -> None:
    _registry.clear()
