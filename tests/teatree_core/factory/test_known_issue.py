"""The operational-health issue registry (PR-17, M6).

``KnownIssue`` is the durable, dedupe-keyed record of one thing the global
aggregator considers wrong. The manager owns the open predicate, the
signal-upsert, the auto-resolve rule, and the two operator verbs.
"""

import pytest

from teatree.core.factory.operational_health import HealthSignal
from teatree.core.models.known_issue import KnownIssue

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _signal(fingerprint: str, *, severity: str = KnownIssue.Severity.WARNING) -> HealthSignal:
    return HealthSignal(
        fingerprint=fingerprint,
        severity=severity,
        summary=f"summary for {fingerprint}",
        kind="stale_tick",
        overlay="teatree",
        evidence_url="https://example.test/evidence",
    )


class TestRecordSignal:
    def test_first_sighting_creates_open_auto_row(self) -> None:
        row = KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        assert row.fingerprint == "stale-tick:loop-a"
        assert row.source == KnownIssue.Source.AUTO
        assert row.evidence_url == "https://example.test/evidence"
        assert row.is_open
        assert KnownIssue.objects.open().count() == 1

    def test_repeat_sighting_updates_one_row_not_two(self) -> None:
        KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        first_seen = KnownIssue.objects.get(fingerprint="stale-tick:loop-a").first_seen
        updated = KnownIssue.objects.record_signal(
            HealthSignal(fingerprint="stale-tick:loop-a", severity=KnownIssue.Severity.CRITICAL, summary="worse now"),
        )
        assert KnownIssue.objects.count() == 1
        assert updated.severity == KnownIssue.Severity.CRITICAL
        assert updated.summary == "worse now"
        assert updated.first_seen == first_seen  # first_seen is sticky
        assert updated.last_seen >= first_seen

    def test_resighting_reopens_a_resolved_auto_row(self) -> None:
        KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        KnownIssue.objects.reconcile(set())  # signal gone -> auto-resolved
        assert KnownIssue.objects.open().count() == 0
        KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        assert KnownIssue.objects.open().count() == 1

    def test_resighting_does_not_reopen_a_dismissed_row(self) -> None:
        row = KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        KnownIssue.objects.dismiss(row.pk)
        KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        assert KnownIssue.objects.open().count() == 0


class TestReconcile:
    def test_auto_resolves_row_whose_signal_cleared(self) -> None:
        KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        KnownIssue.objects.record_signal(_signal("stale-tick:loop-b"))
        resolved = KnownIssue.objects.reconcile({"stale-tick:loop-a"})
        assert resolved == 1
        assert set(KnownIssue.objects.open().values_list("fingerprint", flat=True)) == {"stale-tick:loop-a"}

    def test_never_resolves_a_manual_row(self) -> None:
        KnownIssue.objects.add_manual("operator note")
        KnownIssue.objects.reconcile(set())
        assert KnownIssue.objects.open().count() == 1


class TestOperatorVerbs:
    def test_add_manual_is_open_and_never_auto_resolves(self) -> None:
        row = KnownIssue.objects.add_manual("db snapshot is stale", severity=KnownIssue.Severity.CRITICAL)
        assert row.source == KnownIssue.Source.MANUAL
        assert row.auto_resolve is False
        assert row.severity == KnownIssue.Severity.CRITICAL
        assert row.is_open

    def test_dismiss_closes_an_open_issue(self) -> None:
        row = KnownIssue.objects.record_signal(_signal("stale-tick:loop-a"))
        assert KnownIssue.objects.dismiss(row.pk) is True
        assert KnownIssue.objects.open().count() == 0

    def test_dismiss_absent_returns_false(self) -> None:
        assert KnownIssue.objects.dismiss(9999) is False
