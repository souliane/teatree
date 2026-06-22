"""Scanner-ordering invariant tests for the loop tick.

Two layers are pinned here:

* The per-overlay domain slice ``_messaging_jobs_for_backend`` lists
    ``SlackMentionsScanner`` before ``SlackReviewIntentScanner`` — the former
    drains the JSONL reactions queue the latter consumes.
* ``scan_phase`` must *execute* that pair serially: the dependent scanner
    may not begin until the depended-upon scanner has completed, even though
    every other scanner still fans out across the thread pool. List order
    alone does not guarantee this under a parallel pool.
"""

import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import PER_OVERLAY_DOMAINS, _ScannerJob
from teatree.loop.phases.scan import scan_phase
from teatree.loop.scanners.base import ScanSignal


class ScannerOrderingTestCase(TestCase):
    """Verify scanner ordering assumptions hold under randomization."""

    @staticmethod
    def _backend_with_messaging() -> OverlayBackends:
        host = MagicMock(spec=CodeHostBackend)
        messaging = MagicMock(spec=MessagingBackend)
        return OverlayBackends(
            name="teatree",
            hosts=(host,),
            messaging=messaging,
            ready_labels=("ready",),
            identities=("alice",),
            overlay=MagicMock(),
        )

    def test_slack_mentions_before_review_intent_in_messaging_jobs(self) -> None:
        """SlackMentionsScanner must run before SlackReviewIntentScanner."""
        backend = self._backend_with_messaging()

        scanner_names_by_domain = defaultdict(list)
        for domain in PER_OVERLAY_DOMAINS:
            jobs = jobs_for_domain(domain, backend, all_backends=(backend,))
            scanner_names_by_domain[domain] = [job.scanner.name for job in jobs]

        for domain, names in scanner_names_by_domain.items():
            if "slack_mentions" in names and "slack_review_intent" in names:
                mentions_idx = names.index("slack_mentions")
                review_intent_idx = names.index("slack_review_intent")
                assert mentions_idx < review_intent_idx, (
                    f"In domain {domain}, slack_mentions (index {mentions_idx}) "
                    f"must come before slack_review_intent (index {review_intent_idx})"
                )

    def test_scanner_ordering_with_random_iterations(self) -> None:
        """Run the ordering test 10 times to catch flakes in randomized test order."""
        backend = self._backend_with_messaging()

        for iteration in range(10):
            domain_order = list(PER_OVERLAY_DOMAINS)
            random.shuffle(domain_order)

            scanner_names_by_domain = defaultdict(list)
            for domain in domain_order:
                jobs = jobs_for_domain(domain, backend, all_backends=(backend,))
                scanner_names_by_domain[domain] = [job.scanner.name for job in jobs]

            for domain, names in scanner_names_by_domain.items():
                if "slack_mentions" in names and "slack_review_intent" in names:
                    mentions_idx = names.index("slack_mentions")
                    review_intent_idx = names.index("slack_review_intent")
                    assert mentions_idx < review_intent_idx, (
                        f"Iteration {iteration}, domain {domain}: "
                        f"slack_mentions (index {mentions_idx}) "
                        f"must come before slack_review_intent (index {review_intent_idx})"
                    )


@dataclass(slots=True)
class _QueueProbe:
    populated: threading.Event = field(default_factory=threading.Event)
    intent_started_after_populate: bool = False


@dataclass(slots=True)
class _MentionsProbeScanner:
    probe: _QueueProbe
    name: str = "slack_mentions"

    def scan(self) -> list[ScanSignal]:
        time.sleep(0.05)
        self.probe.populated.set()
        return []


@dataclass(slots=True)
class _ReviewIntentProbeScanner:
    probe: _QueueProbe
    name: str = "slack_review_intent"

    def scan(self) -> list[ScanSignal]:
        self.probe.intent_started_after_populate = self.probe.populated.is_set()
        return []


class ScanPhaseDependencyOrderingTestCase(TestCase):
    """``scan_phase`` must serialize the mentions -> review-intent pair."""

    def test_review_intent_observes_drained_queue(self) -> None:
        probe = _QueueProbe()
        jobs = [
            _ScannerJob(scanner=_ReviewIntentProbeScanner(probe=probe), overlay="teatree"),
            _ScannerJob(scanner=_MentionsProbeScanner(probe=probe), overlay="teatree"),
        ]

        scan_phase(jobs)

        assert probe.intent_started_after_populate, (
            "slack_review_intent ran before slack_mentions finished draining the "
            "reactions queue — the ordering dependency is not enforced at execution time"
        )
