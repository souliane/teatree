"""The drift auditor must never audit its own drift alerts (#1019 recursion guard).

A drift alert is delivered as a Slack DM, and every successful DM records its own
``OutboundClaim``. If the auditor treats that row as auditable, an alert whose own
delivery looks unlanded produces another alert, which produces another — observed
in production as ~7 owner DMs in 4 seconds, each naming the previous one, until the
worker was stopped by hand.

The guard is the ``DRIFT_ALERT_MARKER`` in the alert's idempotency key. It is matched
by CONTAINS rather than prefix because the notify path stores the key under its own
namespace (``notify_user`` writes ``slack_dm:{key}``), so the stored row reads
``slack_dm:outbound_drift:<key>``.
"""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import OutboundClaim
from teatree.loop.scanners.outbound_audit import DRIFT_ALERT_MARKER, OutboundAuditScanner, VerifyResult


def build_claim(idempotency_key: str, *, age_seconds: int = 3600) -> OutboundClaim:
    return OutboundClaim.objects.create(
        idempotency_key=idempotency_key,
        kind=OutboundClaim.Kind.SLACK_DM.value,
        target_url="https://acme.slack.com/archives/D-USER/p1700000000000000",
        claim_ts=timezone.now() - dt.timedelta(seconds=age_seconds),
    )


class TestDriftAlertsAreNotThemselvesAudited(TestCase):
    def _scan_with_always_drifting_verifier(self) -> list[str]:
        alerted: list[str] = []
        scanner = OutboundAuditScanner(
            notifier=lambda _text, key: alerted.append(key),
            verifiers={OutboundClaim.Kind.SLACK_DM.value: lambda _claim: VerifyResult.drift("not found")},
        )
        scanner.scan()
        return alerted

    def test_a_drift_alerts_own_claim_is_never_re_audited(self) -> None:
        # The row as the notify path actually writes it: its OWN namespace in front
        # of the marker. A prefix-anchored guard misses this and the loop runs.
        build_claim(f"slack_dm:{DRIFT_ALERT_MARKER}sess=a;turn=1")

        assert self._scan_with_always_drifting_verifier() == []

    def test_an_ordinary_claim_still_alerts(self) -> None:
        # Anti-vacuity: the guard must silence ONLY drift alerts. If this stops
        # alerting, the auditor has been switched off rather than de-looped.
        build_claim("slack_dm:sess=a;turn=1")

        assert self._scan_with_always_drifting_verifier() == [f"{DRIFT_ALERT_MARKER}slack_dm:sess=a;turn=1"]

    def test_a_bare_marker_key_is_also_excluded(self) -> None:
        # Defends the contract against a caller that does NOT add a namespace.
        build_claim(f"{DRIFT_ALERT_MARKER}sess=a;turn=1")

        assert self._scan_with_always_drifting_verifier() == []
