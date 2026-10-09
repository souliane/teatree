"""Call-time registry for model-callable functions that live ABOVE the models.

``teatree.core.models`` is the lowest stratum of ``core``; gates, overlay
resolvers, and the cost layer all depend ON the models, so a model that called
them directly would form an intra-``core`` up-edge (invisible to tach inside the
single ``core`` node — #2385). The registry inverts the edge: the higher module
registers its callable here at app-ready time, and the model fetches it by name
at call time. The model imports only this leaf (``depends_on = []``), never the
higher module.

One namespaced dict. ``register(kind, name, fn)`` is idempotent — re-registering
the same ``(kind, name)`` overwrites with the new callable, so a second
``AppConfig.ready()`` (test re-entry, ``call_command`` in-process) is a no-op
rather than a duplicate-key error. ``get(kind, name)`` raises a clear
:class:`KeyError` when the registration step never ran, surfacing a wiring bug
loudly instead of silently degrading the FSM gate.
"""

from collections.abc import Callable
from typing import Final

GATE: Final = "gate"
RESOLVER: Final = "resolver"

_REGISTRY: dict[tuple[str, str], Callable[..., object]] = {}


def register(kind: str, name: str, fn: Callable[..., object]) -> None:
    """Register *fn* under ``(kind, name)``; idempotent on re-registration."""
    _REGISTRY[kind, name] = fn


def get(kind: str, name: str) -> Callable[..., object]:
    """Return the callable registered under ``(kind, name)``.

    Raises :class:`KeyError` when nothing is registered — a registration step
    that never ran is a wiring bug, not a silent fallback.
    """
    try:
        return _REGISTRY[kind, name]
    except KeyError:
        msg = (
            f"no {kind!r} registered under {name!r}; the registering package "
            f"(its app-ready import) did not run. Registered {kind}s: "
            f"{sorted(n for k, n in _REGISTRY if k == kind)}"
        )
        raise KeyError(msg) from None


def register_gate(name: str, fn: Callable[..., object]) -> None:
    register(GATE, name, fn)


def get_gate(name: str) -> Callable[..., object]:
    return get(GATE, name)


def register_resolver(name: str, fn: Callable[..., object]) -> None:
    register(RESOLVER, name, fn)


def get_resolver(name: str) -> Callable[..., object]:
    return get(RESOLVER, name)
