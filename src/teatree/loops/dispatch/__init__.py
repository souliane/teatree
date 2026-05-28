"""Dispatch mini-loop — the always-on core that runs every tick.

Carries the global, non-overlay-scoped scanners that have no graceful
degradation path: pending tasks, incoming events, outbound audit. This
mini-loop is :attr:`MiniLoop.always_on` so a user-disabled
``[loops] enabled = false`` cannot silence the tick entirely.
"""
