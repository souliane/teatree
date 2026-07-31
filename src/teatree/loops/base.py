"""MiniLoop dataclass — the per-domain unit the loop-table fan-out dispatches.

A :class:`MiniLoop` is a typed contract every domain package exposes via a
module-level ``MINI_LOOP: MiniLoop`` constant. The loop-table fan-out discovers
these constants via :func:`teatree.loops.registry.iter_loops` and fans out
the unified-verdict-admitted subset on each loop's DB-configured cadence
(:func:`teatree.loops.loop_table.build_loop_table_jobs`).

The ``build_jobs`` callable returns the list of :class:`_ScannerJob`
records (the :mod:`teatree.loop.job_identity` shape) that the fan-out hands
off to the existing :func:`teatree.loop.dispatch.dispatch` pipeline. This
preserves wire compatibility — the loop's plumbing under the fan-out is
unchanged, only the per-domain *grouping* of scan units is new.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.scanners.notion_view import NotionLike


class LoopReach(StrEnum):
    """How far outside this box one loop's tick reaches. Declaration order is render order.

    ``COLLEAGUE`` is a REFINEMENT of ``EGRESS``, not a peer: reaching a person is
    a way of acting outward, so :attr:`MiniLoop.reach` folds ``EGRESS`` in for any
    loop declaring ``COLLEAGUE``. Filtering on ``egress`` therefore catches every
    colleague-reaching loop, while ``colleague`` narrows to the ones whose action
    lands in a human's attention — a Slack post, a DM, a reaction on someone's
    message — where being wrong costs far more than a mis-set label on the forge.
    """

    INGRESS = "ingress"
    EGRESS = "egress"
    COLLEAGUE = "colleague"


class LoopDeterminism(StrEnum):
    """Whether a loop's tick can call a model.

    ``DETERMINISTIC`` is pure code: no model call, no spend, no nondeterminism.
    ``AI`` is any path that dispatches an agent or otherwise calls a model — one
    such path anywhere in the tick is enough, because the tag's whole value is
    telling the owner which loops can never surprise them.
    """

    DETERMINISTIC = "deterministic"
    AI = "ai"


class BuildJobsContext(TypedDict, total=False):
    """The per-tick context the loop-table fan-out spreads into ``build_jobs``.

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
    """One per-domain unit the loop-table fan-out dispatches per tick.

    ``name`` is the durable identity used to match this mini-loop to its DB
    ``Loop`` row — must match the package directory name under
    ``src/teatree/loops/``.

    ``default_cadence_seconds`` is a per-loop seed hint, NOT the live cadence:
    the #2513 cutover made the DB ``Loop`` row's ``delay_seconds`` / ``daily_at``
    the single cadence source the loop-table fan-out (``build_loop_table_jobs`` via
    ``Loop.is_due``) reads. This field records the loop's intended default cadence
    for documentation / seeding; the live tick consults the row, which may differ.

    ``build_jobs`` returns the list of scanner jobs the loop-table fan-out will
    dispatch via the existing :mod:`teatree.loop.dispatch` pipeline. Signature is
    ``**kwargs`` so build callables can accept whichever subset of the per-tick
    context (backends, host, messaging, notion_client, ready_labels) they need.

    ``cadence_is_floor`` marks a loop that gates its own work internally (its
    scanner carries a private cadence or a marker) and whose outer cadence is
    therefore a FLOOR — the fastest sane outer tick, set so the inner cadence
    still fires on time. Slowing such a loop past ``default_cadence_seconds``
    silently starves the inner cadence, so the cadence editor
    (:mod:`teatree.loops.loop_cadence_editing`) treats it as a hard ceiling on
    the interval and refuses a once-a-day wall-clock time.

    ``off_live_tick`` excludes the loop from the live work loop's scanner fan-out
    (:func:`teatree.loops.loop_table.build_loop_table_jobs` skips it) and from the
    loop-timer chains — it is driven by its own ``off_tick_command`` instead, gating on
    the same ``Loop.is_due`` / ``last_run_at`` ledger. Reserved for the heavy passes
    (``dream``, ``directive_loop``, ``outer_loop``) that must not run on or re-arm the
    live tick. Default ``False`` → every existing loop is unchanged.

    ``off_tick_command`` is the management-command argv tail
    (:func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops` runs
    ``python -m teatree <off_tick_command>``) that drives an ``off_live_tick`` loop.
    It is what makes such a loop reachable at all: with the live fan-out and the timer
    chains both excluding it, a loop that declares no command has NO driver — the state
    :func:`teatree.loops.loop_staleness.driverless_loops` alarms on. Meaningless on a
    live-tick loop, which the timer chain already drives.

    ``declared_reach`` and ``determinism`` are the loop's visible tags, and they live
    HERE rather than on the mutable ``Loop`` DB row so no surface can render a
    classification that disagrees with what the code does. Both default to ``None``
    — *undeclared*, which :func:`teatree.loops.classification.unclassified_loops`
    rejects — which keeps an empty ``declared_reach`` available as the real answer
    for a loop touching only this box's own state. Read :attr:`reach`, never
    ``declared_reach``: the property applies the colleague⇒egress implication.
    """

    name: str
    default_cadence_seconds: int
    build_jobs: Callable[..., list["_ScannerJob"]]
    off_live_tick: bool = False
    cadence_is_floor: bool = False
    off_tick_command: tuple[str, ...] = ()
    declared_reach: frozenset[LoopReach] | None = None
    determinism: LoopDeterminism | None = None

    @property
    def reach(self) -> frozenset[LoopReach]:
        declared = self.declared_reach or frozenset()
        if LoopReach.COLLEAGUE in declared:
            return declared | {LoopReach.EGRESS}
        return declared

    @property
    def tags(self) -> tuple[str, ...]:
        reached = tuple(member.value for member in LoopReach if member in self.reach)
        if self.determinism is None:
            return reached
        return (*reached, self.determinism.value)
