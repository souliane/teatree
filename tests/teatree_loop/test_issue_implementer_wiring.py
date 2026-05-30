"""The issue-implementer tick-job builder is gated by the default-OFF triple gate (#1553).

``_issue_implementer_scanner_for`` returns a scanner ONLY when the loop is
opted in for the overlay AND the in-flight concurrency budget has room;
otherwise ``None`` (no job emitted). With the default-OFF config it always
returns ``None``, so the per-overlay ``ISSUE_IMPLEMENTER`` domain slice is
empty and the registry/legacy fan-out stays byte-for-byte unchanged.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.backends.protocols import CodeHostBackend
from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.scanners.issue_implementer import IssueImplementerScanner
from teatree.loop.tick_jobs import Domain, _issue_implementer_scanner_for, jobs_for_domain
from tests.factories import ImplementedIssueMarkerFactory

_PATCH_TARGET = "teatree.loop.tick_jobs._effective_settings_for_overlay"


def _backend(name: str = "acme") -> OverlayBackends:
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        identities=("alice",),
    )


def _settings(**overrides: object) -> UserSettings:
    return UserSettings(**overrides)


class IssueImplementerGateTests(TestCase):
    def test_disabled_by_default_emits_no_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert _issue_implementer_scanner_for(_backend()) is None

    def test_enabled_with_budget_builds_scanner(self) -> None:
        with patch(
            _PATCH_TARGET,
            return_value=_settings(issue_implementer_enabled=True, issue_implementer_label="auto-implement"),
        ):
            scanner = _issue_implementer_scanner_for(_backend())
        assert isinstance(scanner, IssueImplementerScanner)
        assert scanner.label == "auto-implement"
        assert scanner.overlay_name == "acme"
        assert scanner.identities == ("alice",)

    def test_concurrency_at_max_emits_no_scanner(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme")
        with patch(
            _PATCH_TARGET,
            return_value=_settings(
                issue_implementer_enabled=True,
                issue_implementer_label="auto-implement",
                issue_implementer_max_concurrent=1,
            ),
        ):
            assert _issue_implementer_scanner_for(_backend()) is None

    def test_abandoned_marker_does_not_consume_budget(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme", abandoned=True)
        with patch(
            _PATCH_TARGET,
            return_value=_settings(
                issue_implementer_enabled=True,
                issue_implementer_label="auto-implement",
                issue_implementer_max_concurrent=1,
            ),
        ):
            assert _issue_implementer_scanner_for(_backend()) is not None

    def test_budget_is_overlay_scoped(self) -> None:
        ImplementedIssueMarkerFactory(overlay="other")
        with patch(
            _PATCH_TARGET,
            return_value=_settings(
                issue_implementer_enabled=True,
                issue_implementer_label="auto-implement",
                issue_implementer_max_concurrent=1,
            ),
        ):
            assert _issue_implementer_scanner_for(_backend("acme")) is not None

    def test_hostless_backend_emits_no_scanner(self) -> None:
        backend = OverlayBackends(name="acme", hosts=(), messaging=None, ready_labels=())
        with patch(
            _PATCH_TARGET,
            return_value=_settings(issue_implementer_enabled=True, issue_implementer_label="auto-implement"),
        ):
            assert _issue_implementer_scanner_for(backend) is None

    def test_domain_slice_empty_when_disabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert jobs_for_domain(Domain.ISSUE_IMPLEMENTER, _backend()) == []

    def test_domain_slice_emits_one_scanner_when_enabled(self) -> None:
        with patch(
            _PATCH_TARGET,
            return_value=_settings(issue_implementer_enabled=True, issue_implementer_label="auto-implement"),
        ):
            jobs = jobs_for_domain(Domain.ISSUE_IMPLEMENTER, _backend())
        assert [job.scanner.name for job in jobs] == ["issue_implementer"]
        assert jobs[0].overlay == "acme"
