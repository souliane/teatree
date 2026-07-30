"""Process-local warning throttle for per-tick fail-open paths.

A fail-open read that runs on every loop tick — the global-health collectors
(:mod:`teatree.core.factory.operational_health`), the merge slug probe — must not
swallow a *persistent* fault at ``debug`` forever: a health query that keeps
raising, or an overlay registry that will not load, is a real recurring problem
the operator needs to see. Yet promoting the swallow to ``warning`` unconditionally
would emit the same line every beat and drown the log.

:func:`warn_throttled` resolves the tension: the first occurrence of a keyed
failure (and the first after each quiet window) logs at ``warning``, every
recurrence inside the window at ``debug``. A genuinely transient one-off warns
once; a persistent fault stays visible at a steady, bounded cadence; an expected,
frequently-absent miss the caller already knows about should keep calling
``logger.debug`` directly rather than routing through here.
"""

import logging
import time

# Default quiet window: a persistent per-tick failure surfaces at ``warning`` at
# most once every five minutes, so the log shows it is still failing without a
# line every beat.
_DEFAULT_WINDOW_SECONDS = 300.0

# Per-key monotonic timestamp of the last ``warning`` emission. Keys are bounded
# (collector names, overlay names, signal fingerprints), so this never grows
# without bound over a process's life.
_last_warned: dict[str, float] = {}


def warn_throttled(
    logger: logging.Logger,
    key: str,
    msg: str,
    *args: object,
    window_seconds: float = _DEFAULT_WINDOW_SECONDS,
    exc_info: bool = False,
) -> None:
    """Log *msg* at ``warning`` once per *key* per *window_seconds*, else at ``debug``.

    *key* is the stable dedupe identity of the failing site (e.g.
    ``"health-collector:_stale_tick_signals"``). *args* are the ``%``-style
    substitution values for *msg*, exactly as :meth:`logging.Logger.warning`
    takes them. *exc_info* forwards the active exception to the record so a
    warning carries the traceback.
    """
    now = time.monotonic()
    last = _last_warned.get(key)
    if last is not None and now - last < window_seconds:
        logger.debug(msg, *args, exc_info=exc_info)
        return
    _last_warned[key] = now
    logger.warning(msg, *args, exc_info=exc_info)


def reset_throttle() -> None:
    """Clear the per-key window state — test-only, so each case starts un-throttled."""
    _last_warned.clear()
