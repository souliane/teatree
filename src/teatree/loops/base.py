"""MiniLoop dataclass — the per-domain unit the orchestrator dispatches.

A :class:`MiniLoop` is a typed contract every domain package exposes via a
module-level ``MINI_LOOP: MiniLoop`` constant. The orchestrator discovers
these constants via :func:`teatree.loops.registry.iter_loops` and routes
each tick through the enabled subset on its configured cadence.

The ``build_jobs`` callable returns the list of :class:`_ScannerJob`
records (the legacy :mod:`teatree.loop.tick_jobs` shape) that the
orchestrator hands off to the existing :func:`teatree.loop.dispatch.dispatch`
pipeline. This preserves wire compatibility — the loop's plumbing under
the orchestrator is unchanged, only the *grouping* is new.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MiniLoop:
    """One per-domain unit the orchestrator fans out to per tick.

    ``name`` is the durable identity used by the cadence ledger and the
    ``[loops.<name>]`` config table — must match the package directory
    name under ``src/teatree/loops/``.

    ``default_cadence_seconds`` is the floor cadence applied when the
    ``[loops.<name>]`` table omits an explicit ``cadence`` key.

    ``build_jobs`` returns the list of scanner jobs the orchestrator
    will dispatch via the existing :mod:`teatree.loop.dispatch` pipeline.
    Signature is ``**kwargs`` so build callables can accept whichever
    subset of the orchestrator's per-tick context (backends, host,
    messaging, notion_client, ready_labels) they need.

    ``always_on`` keeps a loop enabled even when the user sets
    ``[loops] enabled = false`` — reserved for the core ``dispatch``
    mini-loop which has no graceful-degradation path.
    """

    name: str
    default_cadence_seconds: int
    build_jobs: Callable[..., list[Any]]
    always_on: bool = False
