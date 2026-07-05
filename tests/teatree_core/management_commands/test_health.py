"""The ``t3 <overlay> health`` management command (PR-17).

``show`` reconciles and prints the verdict + open KnownIssue rows with clickable
evidence; ``add`` records a manual issue; ``dismiss`` closes one by id.
"""

import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.models.known_issue import KnownIssue
from teatree.core.operational_health import HealthSignal

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


class TestShow:
    def test_green_when_no_issues(self) -> None:
        with patch("teatree.core.operational_health.collect_signals", return_value=[]):
            out = _call("health", "show")
        assert "health: green · 0 open" in out

    def test_lists_open_issues_with_evidence_link(self) -> None:
        signal = HealthSignal(
            fingerprint="stale-tick:loop-a",
            severity=KnownIssue.Severity.CRITICAL,
            summary="loop wedged",
            overlay="teatree",
            evidence_url="https://example.test/run/9",
        )
        with patch("teatree.core.operational_health.collect_signals", return_value=[signal]):
            out = _call("health", "show")
        assert "health: red · 1 open" in out
        assert "loop wedged" in out
        assert "https://example.test/run/9" in out

    def test_json_output(self) -> None:
        signal = HealthSignal("f", KnownIssue.Severity.WARNING, "a warning")
        with patch("teatree.core.operational_health.collect_signals", return_value=[signal]):
            out = _call("health", "show", "--json")
        payload = json.loads(out)
        assert payload["status"] == "yellow"
        assert payload["open_count"] == 1
        assert payload["issues"][0]["summary"] == "a warning"


class TestAddAndDismiss:
    def test_add_manual_issue(self) -> None:
        out = _call("health", "add", "db snapshot stale", "--critical")
        assert "recorded known-issue" in out
        row = KnownIssue.objects.get(summary="db snapshot stale")
        assert row.severity == KnownIssue.Severity.CRITICAL
        assert row.source == KnownIssue.Source.MANUAL

    def test_dismiss_open_issue(self) -> None:
        row = KnownIssue.objects.add_manual("note")
        out = _call("health", "dismiss", str(row.pk))
        assert f"dismissed known-issue {row.pk}" in out
        assert KnownIssue.objects.open().count() == 0

    def test_dismiss_absent(self) -> None:
        out = _call("health", "dismiss", "9999")
        assert "no open known-issue 9999" in out
