"""The T4 autoresearch outer loop (T4-PR-3).

A single mini-loop that advances at most one :class:`OuterLoopExperiment` one FSM
step per tick — propose → ratify → implement → measure → keep-only-if-better —
consuming the recipe-weighted factory score (T4-PR-2) as its metric-to-beat.

It ships QUADRUPLE-OFF and is a complete no-op at default config: (1) the
``outer_loop_enabled`` feature flag is DARK/off; (2) the seeded ``Loop`` row lands
disabled; (3) ``off_live_tick`` keeps it off the live fan-out; (4) the code guards
G2 (critic-live) / G3 (signal-trust) refuse every tick with a typed reason until a
live critic and honest signals exist. See :mod:`teatree.loops.outer_loop.guards`.
"""
