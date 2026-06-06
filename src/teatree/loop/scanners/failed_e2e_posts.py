"""Failed-E2E Slack-post scanner (#1295 capability E).

Polls one or more Slack channels for *failed-E2E* notification posts and
emits one ``e2e.failure_detected`` signal per extracted spec path. The
overlay supplies one
:class:`teatree.core.overlay.FailedE2EWatcher` per channel: each watcher
declares the post pattern that recognises a failed-E2E notification, the
spec pattern that pulls a spec path out of one bullet line, and the
agent skill the dispatcher should route the signal to (default
``"t3:e2e"``).

The scanner is idempotent through the
:class:`teatree.core.models.ScannedFailedE2E` ledger: each
``(channel, slack_ts, spec_path)`` row is unique so re-ticking on the
same post produces no new signals.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from django.db import OperationalError, ProgrammingError

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import ScannedFailedE2E
from teatree.core.overlay import FailedE2EWatcher
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_broadcasts import ChannelHistoryFetcher
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FailedE2EPostsScanner:
    """Scan configured Slack channels for failed-E2E posts and emit one signal per spec.

    *watchers* is the list of channel watchers — one per channel the
    overlay wants the loop to observe. The scanner iterates each
    watcher, fetches recent messages via
    *fetch_channel_history*, applies the watcher's *post_pattern* to
    decide whether the message is a failed-E2E notification, then runs
    *spec_pattern* over each line to extract every failing spec path.
    """

    backend: MessagingBackend
    watchers: Sequence[FailedE2EWatcher]
    fetch_channel_history: ChannelHistoryFetcher
    overlay: str = ""
    name: str = field(default="failed_e2e_posts", init=False)

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        try:
            for watcher in self.watchers:
                signals.extend(self._scan_watcher(watcher))
        except (OperationalError, ProgrammingError):
            # The ScannedFailedE2E table lives in migration 0033; an
            # install that hasn't migrated yet must not spam a
            # per-tick traceback. Sibling pattern lives in
            # SlackBroadcastsScanner.
            logger.info(
                "FailedE2EPostsScanner: teatree_scanned_failed_e2e unavailable (DB not migrated yet) — skipping",
            )
            return []
        return signals

    def _scan_watcher(self, watcher: FailedE2EWatcher) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        post_re = re.compile(watcher.post_pattern)
        spec_re = re.compile(watcher.spec_pattern)
        for message in self.fetch_channel_history(channel=watcher.channel_id):
            signals.extend(self._handle_message(watcher, post_re, spec_re, message))
        return signals

    def _handle_message(
        self,
        watcher: FailedE2EWatcher,
        post_re: re.Pattern[str],
        spec_re: re.Pattern[str],
        message: RawAPIDict,
    ) -> list[ScanSignal]:
        text = message.get("text")
        ts = message.get("ts")
        if not isinstance(text, str) or not isinstance(ts, str) or not text or not ts:
            return []
        if post_re.search(text) is None:
            return []
        signals: list[ScanSignal] = []
        for line in text.splitlines():
            match = spec_re.search(line)
            if match is None:
                continue
            spec_path = match.group("spec") if "spec" in (match.groupdict() or {}) else match.group(1)
            if not spec_path:
                continue
            row = ScannedFailedE2E.record(
                channel=watcher.channel_id,
                slack_ts=ts,
                spec_path=spec_path,
                test_title=line.strip(),
                overlay=self.overlay,
            )
            if row is None:
                continue
            signals.append(
                ScanSignal(
                    kind="e2e.failure_detected",
                    summary=f"Failed E2E: {spec_path}",
                    payload={
                        "spec": spec_path,
                        "test_title": line.strip(),
                        "channel": watcher.channel_id,
                        "ts": ts,
                        "skill_overlay": self.overlay,
                        "agent_skill": watcher.agent_skill,
                    },
                ),
            )
        return signals


def failed_e2e_scanner_for(backend: object) -> FailedE2EPostsScanner | None:
    """Construct a :class:`FailedE2EPostsScanner` from an :class:`OverlayBackends` (#1295 cap E).

    Returns ``None`` when the overlay has no Python class, no messaging
    backend, or no watchers configured. Moved out of domain_jobs to keep
    that orchestrator under the module-LOC gate.
    """
    from teatree.loop.scanners.slack_broadcasts import BackendChannelHistoryFetcher  # noqa: PLC0415

    overlay = getattr(backend, "overlay", None)
    messaging = getattr(backend, "messaging", None)
    if overlay is None or messaging is None:
        return None
    watchers_getter = getattr(overlay.config, "get_failed_e2e_watchers", None)
    if not callable(watchers_getter):
        return None
    watchers = list(watchers_getter())
    if not watchers:
        return None
    return FailedE2EPostsScanner(
        backend=messaging,
        watchers=watchers,
        fetch_channel_history=BackendChannelHistoryFetcher(backend=messaging),
        overlay=getattr(backend, "name", ""),
    )
