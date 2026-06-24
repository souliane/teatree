"""MiniLoop dataclass — the per-domain unit the orchestrator dispatches.

A :class:`MiniLoop` is a typed contract every domain package exposes via a
module-level ``MINI_LOOP: MiniLoop`` constant. The orchestrator discovers
these constants via :func:`teatree.loops.registry.iter_loops` and routes
each tick through the enabled subset on its configured cadence.

The ``build_jobs`` callable returns the list of :class:`_ScannerJob`
records (the legacy :mod:`teatree.loop.job_identity` shape) that the
orchestrator hands off to the existing :func:`teatree.loop.dispatch.dispatch`
pipeline. This preserves wire compatibility — the loop's plumbing under
the orchestrator is unchanged, only the *grouping* is new.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.scanners.notion_view import NotionLike


class BuildJobsContext(TypedDict, total=False):
    """The per-tick context the orchestrator spreads into ``build_jobs``.

    Mirrors :class:`teatree.loop.tick.TickRequest`'s fields. ``total=False``
    because each mini-loop's ``build_jobs`` accepts only the subset of
    keys it needs (the rest are swallowed by its ``**_`` catch-all), and
    the live tick's single-overlay path omits ``backends``.
    """

    backends: "list[OverlayBackends] | None"
    host: "CodeHostBackend | None"
    messaging: "MessagingBackend | None"
    notion_client: "NotionLike | None"
    ready_labels: tuple[str, ...]


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

    ``always_on`` keeps a loop enabled even when the env kill-switch
    ``T3_LOOPS_DISABLED`` would otherwise disable it — reserved for the
    core ``dispatch`` mini-loop which has no graceful-degradation path.
    Only an explicit DB ``LoopState`` pause/disable can stop an
    ``always_on`` loop (the env layer cannot; #2702 removed the former
    ``[loops] enabled`` toml fallback).

    ``off_live_tick`` excludes the loop from the live 12-minute work loop's
    scanner fan-out (and the orchestrator's normal dispatch) — it is driven
    by its OWN low-frequency cron instead, while still being registered so
    its cadence is configured under ``[loops.<name>]``. Reserved for the
    heavy ``dream`` consolidation pass (#1933 § 3), which must not run on or
    re-arm the live tick. Default ``False`` → every existing loop is
    unchanged.
    """

    name: str
    default_cadence_seconds: int
    build_jobs: Callable[..., list["_ScannerJob"]]
    always_on: bool = False
    off_live_tick: bool = False
