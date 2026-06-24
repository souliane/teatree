"""Dispatch mini-loop — the always-on core that runs every tick.

Carries the global, non-overlay-scoped scanners that have no graceful
degradation path: pending tasks, incoming events, outbound audit. This
mini-loop is :attr:`MiniLoop.always_on` so the ``T3_LOOPS_DISABLED`` env
kill-switch cannot silence the tick entirely (only an explicit DB
``LoopState`` pause/disable can; #2702 removed the ``[loops] enabled`` toml
fallback).
"""
