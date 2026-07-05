"""Per-loop cadence / TTL readers — a pure leaf over ``os.environ`` (#2413 PR-4).

The dedicated loop-line dashboard (:mod:`teatree.loop.statusline_loops`, a low
presentation module) colors each loop's next-tick countdown against that loop's
own cadence, so it must resolve every loop's cadence. These readers depend on
nothing but ``os.environ``, so they live at the bottom of the ``teatree.loop``
layer and every consumer reaches them via an eager DOWN edge: the ``loops_tick``
per-loop tick command (``loop_owner_ttl_seconds``), the reactive drain-queue
command (``drain_cadence_seconds``), and ``statusline_loops`` — no back-edge, no
re-export shim.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass

_LOOP_OWNER_TTL_DEFAULT = 1800
_SECONDS_PER_MINUTE = 60


def slack_answer_cadence_seconds() -> int:
    """The ``loop-slack-answer`` throttle window (``T3_SLACK_ANSWER_CADENCE``, default 20s, floor 15)."""
    raw = os.environ.get("T3_SLACK_ANSWER_CADENCE", "20").strip() or "20"
    try:
        return max(15, int(raw))
    except ValueError:
        return 20


def self_improve_cadence_seconds() -> int:
    """The ``loop-self-improve`` throttle window (``T3_SELF_IMPROVE_CHEAP_CADENCE``, default 1800s, floor 60)."""
    raw = os.environ.get("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800").strip() or "1800"
    try:
        return max(60, int(raw))
    except ValueError:
        return 1800


def loop_owner_ttl_seconds() -> int:
    """The persistent ``t3-master`` claim TTL (``T3_LOOP_OWNER_TTL``, default 1800s, floor 60).

    A blank or non-integer override degrades to the default rather than crashing
    the tick; the 60s floor keeps a fat-fingered tiny TTL from making the owner
    lapse mid-tick.
    """
    raw = os.environ.get("T3_LOOP_OWNER_TTL", str(_LOOP_OWNER_TTL_DEFAULT)).strip()
    if not raw:
        return _LOOP_OWNER_TTL_DEFAULT
    try:
        return max(60, int(raw))
    except ValueError:
        return _LOOP_OWNER_TTL_DEFAULT


def drain_cadence_seconds() -> int:
    """The ``loop-drain-queue`` throttle window (``T3_QUEUE_DRAIN_CADENCE``, default 30s, floor 10)."""
    raw = os.environ.get("T3_QUEUE_DRAIN_CADENCE", "30").strip() or "30"
    try:
        return max(10, int(raw))
    except ValueError:
        return 30


@dataclass(frozen=True, slots=True)
class ReactiveSlot:
    """A reactive infra ``/loop`` slot — sub-minute, so it registers via the ``/loop <duration>`` form (#2650).

    The three reactive slots (Slack-answer, self-improve, drain-queue) have NO DB
    ``Loop`` row: their sub-minute cadence cannot be a minute-granular cron, so
    each is its OWN dedicated ``/loop`` on a *duration* cadence. This bundles a
    slot's cadence reader (the SoT for its throttle seconds, above) with the
    ``t3 loop <slot> run`` it fires, so ``t3 loop <slot> start`` AND the
    owner-session bootstrap (:mod:`hooks.scripts.loop_registrations`) register the
    SAME ``/loop`` — there is no master tick to piggyback the cycles onto, so the
    owner registers these three directly.
    """

    slot_id: str
    cadence_seconds: Callable[[], int]
    run_command: str

    def cadence(self) -> str:
        """The ``/loop`` duration token — ``<N>m`` when minute-aligned, else ``<N>s``."""
        seconds = self.cadence_seconds()
        if seconds % _SECONDS_PER_MINUTE == 0:
            return f"{seconds // _SECONDS_PER_MINUTE}m"
        return f"{seconds}s"

    def loop_directive(self) -> str:
        """The ``/loop <duration> Run `...`.`` slash command that registers this reactive slot."""
        return f"/loop {self.cadence()} Run `{self.run_command}`."


#: The three reactive infra ``/loop`` slots, in registration order — the single
#: source of truth both ``t3 loop <slot> start`` and the owner bootstrap read.
REACTIVE_SLOTS: tuple[ReactiveSlot, ...] = (
    ReactiveSlot("loop-slack-answer", slack_answer_cadence_seconds, "t3 loop slack-answer run"),
    ReactiveSlot("loop-self-improve", self_improve_cadence_seconds, "t3 loop self-improve run --tier cheap"),
    ReactiveSlot("loop-drain-queue", drain_cadence_seconds, "t3 loop drain-queue run"),
)

_REACTIVE_BY_SLOT: dict[str, ReactiveSlot] = {slot.slot_id: slot for slot in REACTIVE_SLOTS}


def reactive_slot(slot_id: str) -> ReactiveSlot:
    """The reactive slot for ``loop-slack-answer`` / ``loop-self-improve`` / ``loop-drain-queue``."""
    return _REACTIVE_BY_SLOT[slot_id]


def reactive_slot_directives() -> list[str]:
    """The ``/loop <duration>`` registrations for all three reactive infra loops (owner-session bootstrap)."""
    return [slot.loop_directive() for slot in REACTIVE_SLOTS]


__all__ = [
    "REACTIVE_SLOTS",
    "ReactiveSlot",
    "drain_cadence_seconds",
    "loop_owner_ttl_seconds",
    "reactive_slot",
    "reactive_slot_directives",
    "self_improve_cadence_seconds",
    "slack_answer_cadence_seconds",
]
