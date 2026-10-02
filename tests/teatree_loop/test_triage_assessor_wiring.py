"""The triage-assessor tick-job builder, and what it threads into the scanner.

Whether the loop runs at all is the active preset's opinion, so this builder answers a
narrower question: given the loop admitted, does it emit exactly one scanner carrying the
settings it was given, and nothing at all for an overlay with no code host.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import NEEDS_TRIAGE_LABEL
from teatree.core.models.task import Task
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain
from teatree.loop.scanner_factories import _triage_assessor_scanner_for
from teatree.loop.scanners.triage_assessor import TriageAssessorScanner

_PATCH_TARGET = "teatree.loop.scanner_factories._effective_settings_for_overlay"
_URL = "https://github.com/souliane/teatree/issues/800"


def _settings(**overrides: object) -> UserSettings:
    return UserSettings(**overrides)


def _backend(name: str = "acme") -> OverlayBackends:
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        identities=("alice",),
    )


def _issue_host() -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.current_user.return_value = "alice"
    host.list_assigned_issues.return_value = [
        {"web_url": _URL, "title": "open needs-triage issue", "labels": [NEEDS_TRIAGE_LABEL], "state": "open"}
    ]
    return host


def _backend_with_host(host: CodeHostBackend, name: str = "acme") -> OverlayBackends:
    overlay = MagicMock()
    overlay.review.merge_candidate_repo_slugs.return_value = ("souliane/teatree",)
    return OverlayBackends(
        name=name,
        hosts=(host,),
        messaging=None,
        ready_labels=(),
        identities=("alice",),
        overlay=overlay,
    )


class TriageAssessorGateTests(TestCase):
    def test_enabled_builds_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            scanner = _triage_assessor_scanner_for(_backend())
        assert isinstance(scanner, TriageAssessorScanner)
        assert scanner.overlay_name == "acme"
        assert scanner.identities == ("alice",)
        assert scanner.max_issues_per_tick == 10

    def test_hostless_backend_emits_no_scanner(self) -> None:
        backend = OverlayBackends(name="acme", hosts=(), messaging=None, ready_labels=())
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert _triage_assessor_scanner_for(backend) is None

    def test_domain_slice_emits_one_scanner_when_enabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            jobs = jobs_for_domain(Domain.TRIAGE_ASSESSOR, _backend())
        assert [job.scanner.name for job in jobs] == ["triage_assessor"]
        assert jobs[0].overlay == "acme"


class TriageAssessorQueuesOneTaskTests(TestCase):
    """An OPEN needs-triage issue queues exactly ONE shell-denied assessment task."""

    def test_an_open_issue_queues_one_assessment_task(self) -> None:
        host = _issue_host()
        with patch(_PATCH_TARGET, return_value=_settings()):
            jobs = jobs_for_domain(Domain.TRIAGE_ASSESSOR, _backend_with_host(host))
        signals = [signal for job in jobs for signal in job.scanner.scan()]
        assert [s.kind for s in signals] == ["triage_assessor.queued"]
        assert Task.objects.filter(phase="triage_assessing").count() == 1
        host.list_assigned_issues.assert_called_once_with(
            assignee="alice",
            repo_slugs=("souliane/teatree",),
        )
