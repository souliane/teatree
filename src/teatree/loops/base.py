"""MiniLoop dataclass — the per-domain unit the master tick dispatches.

A :class:`MiniLoop` is a typed contract every domain package exposes via a
module-level ``MINI_LOOP: MiniLoop`` constant. The master tick discovers
these constants via :func:`teatree.loops.registry.iter_loops` and fans out
the unified-verdict-admitted subset on each loop's DB-configured cadence
(:func:`teatree.loops.master.build_loop_table_jobs`).

The ``build_jobs`` callable returns the list of :class:`_ScannerJob`
records (the :mod:`teatree.loop.job_identity` shape) that the master hands
off to the existing :func:`teatree.loop.dispatch.dispatch` pipeline. This
preserves wire compatibility — the loop's plumbing under the master tick is
unchanged, only the per-domain *grouping* of scan units is new.
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
    """The per-tick context the master tick spreads into ``build_jobs``.

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
    """One per-domain unit the master tick fans out to per tick.

    ``name`` is the durable identity used to match this mini-loop to its DB
    ``Loop`` row — must match the package directory name under
    ``src/teatree/loops/``.

    ``default_cadence_seconds`` is a per-loop seed hint, NOT the live cadence:
    the #2513 cutover made the DB ``Loop`` row's ``delay_seconds`` / ``daily_at``
    the single cadence source the master tick (``build_loop_table_jobs`` via
    ``Loop.is_due``) reads. This field records the loop's intended default cadence
    for documentation / seeding; the live tick consults the row, which may differ.

    ``build_jobs`` returns the list of scanner jobs the master tick will dispatch
    via the existing :mod:`teatree.loop.dispatch` pipeline. Signature is
    ``**kwargs`` so build callables can accept whichever subset of the per-tick
    context (backends, host, messaging, notion_client, ready_labels) they need.

    ``off_live_tick`` excludes the loop from the live work loop's scanner fan-out
    (:func:`teatree.loops.master.build_loop_table_jobs` skips it) — it is driven
    by its OWN low-frequency cron instead, gating on the same ``Loop.is_due`` /
    ``last_run_at`` ledger. Reserved for the heavy ``dream`` consolidation pass
    (#1933 § 3), which must not run on or re-arm the live tick. Default ``False``
    → every existing loop is unchanged.
    """

    name: str
    default_cadence_seconds: int
    build_jobs: Callable[..., list["_ScannerJob"]]
    off_live_tick: bool = False
