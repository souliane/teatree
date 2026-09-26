"""Durable ``SelfImproveFiring`` row ops (BLUEPRINT § 5.7).

Mirrors the style of ``loop/persistence.py``: a thin functional surface
that translates detector reports into DB writes through
``transaction.atomic()``.  The dedup contract is enforced by the unique
constraint on ``(detector, dedup_key)``; this module handles the read +
update side (record a new firing, count recent Slack firings, look up
the last firing for a key).
"""

import datetime as dt
import hashlib
import logging

from django.db import transaction
from django.db.models import Case, F, Value, When
from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve.detectors.base import DetectorReport, DetectorScan

logger = logging.getLogger(__name__)


# Global Slack rate cap (one self-improve DM per 30 min, regardless of
# detector). The integration with `actions.format_slack_payload` consults
# `recent_slack_firings_within(SLACK_RATE_CAP_SECONDS)` and downgrades
# the rung to ``statusline`` when the cap is hit.
SLACK_RATE_CAP_SECONDS = 30 * 60


def latest_firing(detector: str, dedup_key: str) -> SelfImproveFiring | None:
    """Return the persisted firing for ``(detector, dedup_key)`` or ``None``."""
    return SelfImproveFiring.objects.filter(detector=detector, dedup_key=dedup_key).first()


def resolve_absent_firings(
    detector: str,
    scan: DetectorScan,
    *,
    observed_at: dt.datetime,
    key_prefix: str | None = None,
) -> None:
    """Close only absences covered by authoritative source evidence."""
    if not scan.complete and not (scan.protected_keys or scan.protected_prefixes or scan.candidate_keys is not None):
        return
    if scan.candidate_keys is not None and not scan.candidate_keys:
        return
    firings = SelfImproveFiring.objects.filter(
        detector=detector,
        resolved_at__isnull=True,
        last_fired_at__lte=observed_at,
    )
    if key_prefix is not None:
        firings = firings.filter(dedup_key__startswith=key_prefix)
    if scan.candidate_keys is not None:
        firings = firings.filter(dedup_key__in=scan.candidate_keys)
    if scan.protected_keys:
        firings = firings.exclude(dedup_key__in=scan.protected_keys)
    for prefix in scan.protected_prefixes:
        firings = firings.exclude(dedup_key__startswith=prefix)
    observed_keys = {report.dedup_key for report in scan.reports} - scan.reopen_keys
    firings.exclude(dedup_key__in=observed_keys).update(resolved_at=timezone.now())


def record_firing(
    report: DetectorReport,
    *,
    action: str,
    now: dt.datetime | None = None,
) -> SelfImproveFiring:
    """Insert or update the durable firing row for ``report``.

    New row: ``get_or_create`` with ``action_count=1``.  Existing row: a
    single ``filter().update(action_count=F(...)+1)`` so the increment is
    evaluated by the DB and concurrent ticks cannot lose-update the counter.
    ``action_count`` increments on every observation — the monotonic counter
    is the post-hoc telemetry signal.
    """
    moment = now or timezone.now()
    digest = hashlib.sha256(report.dedup_key.encode()).hexdigest()[:16]
    with transaction.atomic():
        firing, created = SelfImproveFiring.objects.get_or_create(
            detector=report.detector,
            dedup_key=report.dedup_key,
            defaults={
                "dedup_key_digest": digest,
                "state_hash": report.state_hash,
                "severity": report.severity,
                "first_fired_at": moment,
                "last_fired_at": moment,
                "last_action": action,
                "payload": report.payload,
                "action_count": 1,
            },
        )
        if not created:
            SelfImproveFiring.objects.filter(pk=firing.pk).update(
                dedup_key_digest=digest,
                state_hash=report.state_hash,
                severity=report.severity,
                first_fired_at=Case(
                    When(resolved_at__isnull=False, then=Value(moment)),
                    default=F("first_fired_at"),
                ),
                last_fired_at=moment,
                last_action=action,
                payload=report.payload,
                action_count=F("action_count") + 1,
                ticket=Case(When(resolved_at__isnull=False, then=Value(None)), default=F("ticket")),
                resolved_at=None,
            )
            firing.refresh_from_db()
    return firing


def recent_slack_firings_within(seconds: int, *, now: dt.datetime | None = None) -> int:
    """Count Slack-rung firings inside the trailing ``seconds`` window.

    Used by ``actions.format_slack_payload`` to enforce the global rate
    cap — the count is detector-agnostic by design (one cap across the
    whole monitor).
    """
    moment = now or timezone.now()
    cutoff = moment - dt.timedelta(seconds=seconds)
    return SelfImproveFiring.objects.filter(
        last_action=SelfImproveFiring.Action.SLACK,
        last_fired_at__gte=cutoff,
    ).count()
