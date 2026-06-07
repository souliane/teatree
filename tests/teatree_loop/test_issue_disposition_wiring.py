"""The issue-disposition tick-job builder is gated default-OFF (#2122).

``_issue_disposition_scanner_for`` returns a scanner ONLY when
``auto_disposition_enabled`` is flipped on for the overlay; otherwise ``None``
(no job emitted). With the default-OFF config the per-overlay
``ISSUE_DISPOSITION`` domain slice is empty and the fan-out stays unchanged.

The default-OFF gate is anti-vacuity guard (b): with the flag OFF the scanner
emits nothing even given a dead issue — removing the gate makes this go RED.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import NEEDS_TRIAGE_LABEL, Ticket
from teatree.loop.dispatch import dispatch
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain
from teatree.loop.scanner_factories import _issue_disposition_scanner_for
from teatree.loop.scanners.issue_disposition import CLOSE_CANDIDATE_KIND, IssueDispositionScanner

_PATCH_TARGET = "teatree.loop.scanner_factories._effective_settings_for_overlay"
_DEAD_URL = "https://github.com/souliane/teatree/issues/700"


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


def _dead_host() -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.current_user.return_value = "alice"
    host.list_assigned_issues.return_value = [
        {"web_url": _DEAD_URL, "title": "shipped already", "labels": [NEEDS_TRIAGE_LABEL], "state": "open"}
    ]
    host.search_open_issues.return_value = []
    return host


def _backend_with_host(host: CodeHostBackend, name: str = "acme") -> OverlayBackends:
    return OverlayBackends(name=name, hosts=(host,), messaging=None, ready_labels=(), identities=("alice",))


class IssueDispositionGateTests(TestCase):
    def test_disabled_by_default_emits_no_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert _issue_disposition_scanner_for(_backend()) is None

    def test_enabled_builds_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings(auto_disposition_enabled=True)):
            scanner = _issue_disposition_scanner_for(_backend())
        assert isinstance(scanner, IssueDispositionScanner)
        assert scanner.overlay_name == "acme"
        assert scanner.identities == ("alice",)
        assert scanner.max_closes_per_tick == 5

    def test_max_closes_per_tick_threads_through(self) -> None:
        with patch(
            _PATCH_TARGET,
            return_value=_settings(auto_disposition_enabled=True, auto_disposition_max_closes_per_tick=2),
        ):
            scanner = _issue_disposition_scanner_for(_backend())
        assert scanner is not None
        assert scanner.max_closes_per_tick == 2

    def test_hostless_backend_emits_no_scanner(self) -> None:
        backend = OverlayBackends(name="acme", hosts=(), messaging=None, ready_labels=())
        with patch(_PATCH_TARGET, return_value=_settings(auto_disposition_enabled=True)):
            assert _issue_disposition_scanner_for(backend) is None

    def test_domain_slice_empty_when_disabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend()) == []

    def test_domain_slice_emits_one_scanner_when_enabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings(auto_disposition_enabled=True)):
            jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend())
        assert [job.scanner.name for job in jobs] == ["issue_disposition"]
        assert jobs[0].overlay == "acme"


class IssueDispositionGateAntiVacuityTests(TestCase):
    """Anti-vacuity (b): the flag OFF emits NOTHING even for a genuinely dead issue."""

    def setUp(self) -> None:
        Ticket.objects.create(issue_url=_DEAD_URL, state=Ticket.State.DELIVERED)

    def test_flag_off_emits_nothing_for_dead_issue(self) -> None:
        host = _dead_host()
        with patch(_PATCH_TARGET, return_value=_settings()):
            jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend_with_host(host))
        signals = [signal for job in jobs for signal in job.scanner.scan()]
        assert signals == []

    def test_flag_on_emits_close_candidate_and_routes_to_mechanical(self) -> None:
        host = _dead_host()
        with patch(_PATCH_TARGET, return_value=_settings(auto_disposition_enabled=True)):
            jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend_with_host(host))
        signals = [signal for job in jobs for signal in job.scanner.scan()]
        assert [s.kind for s in signals] == [CLOSE_CANDIDATE_KIND]

        actions = dispatch(signals)
        assert [(a.kind, a.zone) for a in actions] == [("mechanical", "close_dead_issue")]
