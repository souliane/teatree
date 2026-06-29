"""Per-loop cadence / TTL readers — a pure leaf over ``os.environ`` (#2413 PR-4).

The dedicated loop-line dashboard (:mod:`teatree.loop.statusline_loops`, a low
presentation module) colors each loop's next-tick countdown against that loop's
own cadence, so it must resolve every loop's cadence. The readers used to live
up-stack on :mod:`teatree.loop.tick_piggyback` / :mod:`teatree.loop.queue_drain`
(the orchestration top), forcing ``statusline_loops`` to *defer*-import UP into
them — a back-edge hidden from tach's acyclic guard.

These readers depend on nothing but ``os.environ``, so they belong at the bottom
of the ``teatree.loop`` layer. Pulling them into this leaf lets ``statusline_loops``
reach them via an eager DOWN edge; ``tick_piggyback`` / ``queue_drain`` re-export
them so every existing import path (the ``loops_tick`` management command resolves
``_loop_owner_ttl_seconds`` from ``tick_piggyback``) is unchanged.
"""

import os

_LOOP_OWNER_TTL_DEFAULT = 1800


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
    """The persistent ``loop-owner`` claim TTL (``T3_LOOP_OWNER_TTL``, default 1800s, floor 60).

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


__all__ = [
    "drain_cadence_seconds",
    "loop_owner_ttl_seconds",
    "self_improve_cadence_seconds",
    "slack_answer_cadence_seconds",
]
