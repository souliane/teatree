"""The :class:`FailedE2EWatcher` spec — capability E (#1295).

A pure value object split out of :mod:`teatree.core.overlay` so the seam module
stays under the per-file LOC cap. Re-exported from ``teatree.core.overlay`` for
back-compat, so ``from teatree.core.overlay import FailedE2EWatcher`` keeps working.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FailedE2EWatcher:
    """One Slack-channel watcher spec for capability E (#1295).

    The loop's ``FailedE2EPostsScanner`` consumes a list of these from
    :meth:`~teatree.core.overlay.OverlayConfig.get_failed_e2e_watchers`; each
    watcher tells the scanner which channel to poll, how to recognise a
    failed-E2E post in that channel, how to extract the failing spec path from
    one bullet, and which agent skill to dispatch with the extracted spec.

    ``post_pattern`` is a regex applied to the *message text* — a match
    means "this is a failed-E2E post". ``spec_pattern`` is a regex
    applied to one bullet line and must yield the spec path in either
    group(1) or the named group ``spec``; non-matching bullets are
    skipped. ``agent_skill`` is the skill name (e.g. ``"t3:e2e"``) the
    dispatcher routes the resulting signal to.
    """

    channel_id: str
    post_pattern: str
    spec_pattern: str
    agent_skill: str = "t3:e2e"
