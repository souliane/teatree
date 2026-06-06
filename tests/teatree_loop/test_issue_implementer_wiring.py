"""The issue-implementer tick-job builder is gated by the default-OFF triple gate (#1553).

``_issue_implementer_scanner_for`` returns a scanner ONLY when the loop is
opted in for the overlay AND the in-flight concurrency budget has room;
otherwise ``None`` (no job emitted). With the default-OFF config it always
returns ``None``, so the per-overlay ``ISSUE_IMPLEMENTER`` domain slice is
empty and the registry/legacy fan-out stays byte-for-byte unchanged.

C4 (#1554) wires the mini-loop into the live tick and routes the emitted
``issue_implementer.claimed`` signal to ``t3:orchestrator`` (maker-side
kickoff). These tests pin the default-OFF inertness end-to-end, the
enabled→dispatch path, and the empty-label operator warning.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.backends.protocols import CodeHostBackend
from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.dispatch import dispatch
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain
from teatree.loop.scanner_factories import _issue_implementer_scanner_for
from teatree.loop.scanners.issue_implementer import IssueImplementerScanner
from teatree.loops.issue_implementer.loop import MINI_LOOP
from tests.factories import ImplementedIssueMarkerFactory

_PATCH_TARGET = "teatree.loop.scanner_factories._effective_settings_for_overlay"


def _backend(name: str = "acme") -> OverlayBackends:
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        identities=("alice",),
    )


def _labelled_host(*urls: str, label: str = "auto-implement") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.current_user.return_value = "alice"
    host.list_assigned_issues.return_value = [
        {"web_url": url, "title": f"do {url}", "labels": [label], "state": "open"} for url in urls
    ]
    return host


def _backend_with_host(host: CodeHostBackend, name: str = "acme") -> OverlayBackends:
    return OverlayBackends(name=name, hosts=(host,), messaging=None, ready_labels=(), identities=("alice",))


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


class IssueImplementerEmptyLabelWarningTests(TestCase):
    """Enabled + empty label is a safe but silent no-op — warn the operator (#1554)."""

    def test_enabled_empty_label_emits_no_scanner_and_warns(self) -> None:
        with (
            patch(_PATCH_TARGET, return_value=_settings(issue_implementer_enabled=True)),
            self.assertLogs("teatree.loop.scanner_factories", level="WARNING") as captured,
        ):
            scanner = _issue_implementer_scanner_for(_backend("acme"))
        assert scanner is None
        joined = "\n".join(captured.output)
        assert "issue_implementer_label is empty" in joined
        assert "acme" in joined

    def test_enabled_with_label_does_not_warn(self) -> None:
        with (
            patch(
                _PATCH_TARGET,
                return_value=_settings(issue_implementer_enabled=True, issue_implementer_label="auto-implement"),
            ),
            self.assertNoLogs("teatree.loop.scanner_factories", level="WARNING"),
        ):
            assert _issue_implementer_scanner_for(_backend()) is not None

    def test_disabled_empty_label_does_not_warn(self) -> None:
        with (
            patch(_PATCH_TARGET, return_value=_settings()),
            self.assertNoLogs("teatree.loop.scanner_factories", level="WARNING"),
        ):
            assert _issue_implementer_scanner_for(_backend()) is None


class IssueImplementerMiniLoopTests(TestCase):
    """The mini-loop is the live-tick entry point — enabled→dispatch, disabled→inert (#1554)."""

    def test_mini_loop_identity(self) -> None:
        assert MINI_LOOP.name == "issue_implementer"
        assert MINI_LOOP.always_on is False

    def test_disabled_loop_is_inert(self) -> None:
        host = _labelled_host("https://github.com/souliane/teatree/issues/100")
        with patch(_PATCH_TARGET, return_value=_settings()):
            jobs = MINI_LOOP.build_jobs(backends=[_backend_with_host(host)])
        assert jobs == []

    def test_no_backends_is_inert(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings(issue_implementer_enabled=True)):
            assert MINI_LOOP.build_jobs(backends=None) == []

    def test_enabled_loop_claims_and_dispatches_to_orchestrator(self) -> None:
        url = "https://github.com/souliane/teatree/issues/100"
        host = _labelled_host(url)
        with patch(
            _PATCH_TARGET,
            return_value=_settings(issue_implementer_enabled=True, issue_implementer_label="auto-implement"),
        ):
            jobs = MINI_LOOP.build_jobs(backends=[_backend_with_host(host)])
        assert [job.scanner.name for job in jobs] == ["issue_implementer"]

        signals = [signal for job in jobs for signal in job.scanner.scan()]
        claimed = [s for s in signals if s.kind == "issue_implementer.claimed"]
        assert [s.payload["url"] for s in claimed] == [url]

        actions = dispatch(claimed)
        agent_zones = [a.zone for a in actions if a.kind == "agent"]
        assert agent_zones == ["t3:orchestrator"]
        assert any(a.kind == "statusline" and a.zone == "action_needed" for a in actions)
