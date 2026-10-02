"""Dispatch mini-loop — the core that runs every tick.

Carries the global, non-overlay-scoped scanners that have no graceful
degradation path: pending tasks, incoming events, outbound audit. Like
every loop, it is silenced only by an explicit DB ``LoopState``
pause/disable (``t3 loop pause``/``disable`` dispatch) — there is no env
kill-switch and no ``[loops]`` toml disabled-state fallback.
"""
