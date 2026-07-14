"""The issue-implementer tick-job builder is gated by the default-OFF triple gate (#1553, #3235).

``_issue_implementer_scanner_for`` returns a scanner ONLY when the loop is
opted in for the overlay AND the in-flight concurrency budget has room;
otherwise ``None`` (no job emitted). With the default-OFF config it always
returns ``None``, so the per-overlay ``ISSUE_IMPLEMENTER`` domain slice is
empty and the registry/legacy fan-out stays byte-for-byte unchanged.

C4 (#1554) wires the mini-loop into the live tick and routes the emitted
``issue_implementer.claimed`` signal to ``t3:orchestrator`` (maker-side
kickoff). #3235 moves INTAKE from a hand-applied label to the trusted AUTHOR of
the issue: the builder resolves the effective trusted-author set from config and
hands it to the scanner, and the label filter only applies when the operator
opts back into it with ``issue_implementer_require_label``.

These tests pin the default-OFF inertness end-to-end, the enabled→dispatch path
(unlabelled, trusted-author issues), and the require-label operator warning.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import Task, Ticket
from teatree.loop.dispatch import dispatch
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain
from teatree.loop.persistence import persist_agent_actions
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


def _authored_host(*urls: str, author: str = "alice") -> CodeHostBackend:
    """A host whose author-scoped issue query returns *urls*, all authored by *author*."""
    host = MagicMock(spec=CodeHostBackend)
    host.current_user.return_value = "alice"
    host.list_authored_issues.return_value = [
        {"web_url": url, "title": f"do {url}", "labels": [], "state": "open", "user": {"login": author}} for url in urls
    ]
    host.list_my_prs.return_value = []
    host.list_my_merged_prs.return_value = []
    return host


def _backend_with_host(host: CodeHostBackend, name: str = "acme") -> OverlayBackends:
    return OverlayBackends(name=name, hosts=(host,), messaging=None, ready_labels=(), identities=("alice",))


def _settings(**overrides: object) -> UserSettings:
    return UserSettings(**overrides)


def _enabled(**overrides: object) -> UserSettings:
    """The enabled loop with one trusted author — the #3235 baseline posture."""
    return _settings(issue_implementer_enabled=True, user_identity_aliases=["alice"], **overrides)


class IssueImplementerGateTests(TestCase):
    def test_disabled_by_default_emits_no_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert _issue_implementer_scanner_for(_backend()) is None

    def test_enabled_with_budget_builds_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_label="auto-implement")):
            scanner = _issue_implementer_scanner_for(_backend())
        assert isinstance(scanner, IssueImplementerScanner)
        assert scanner.label == "auto-implement"
        assert scanner.overlay_name == "acme"
        assert scanner.identities == ("alice",)

    def test_enabled_without_a_label_still_builds_a_scanner(self) -> None:
        """#3235: an unset label is no longer a kill-switch — intake is by trusted author."""
        with patch(_PATCH_TARGET, return_value=_enabled()):
            scanner = _issue_implementer_scanner_for(_backend())
        assert isinstance(scanner, IssueImplementerScanner)
        assert scanner.require_label is False
        assert scanner.can_claim is True

    def test_trusted_authors_are_resolved_from_the_config_union(self) -> None:
        """The builder hands the scanner the UNION of aliases + the ``trusted_issue_authors`` allowlist."""
        settings = _settings(
            issue_implementer_enabled=True,
            user_identity_aliases=["souliane"],
            trusted_issue_authors=["adrien-oper"],
        )
        with patch(_PATCH_TARGET, return_value=settings):
            scanner = _issue_implementer_scanner_for(_backend())
        assert isinstance(scanner, IssueImplementerScanner)
        assert set(scanner.trusted_authors) == {"souliane", "adrien-oper"}

    def test_require_label_flag_is_plumbed_to_the_scanner(self) -> None:
        with patch(
            _PATCH_TARGET,
            return_value=_enabled(issue_implementer_label="auto-implement", issue_implementer_require_label=True),
        ):
            scanner = _issue_implementer_scanner_for(_backend())
        assert isinstance(scanner, IssueImplementerScanner)
        assert scanner.require_label is True

    def test_concurrency_at_max_emits_no_scanner(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme")
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)):
            assert _issue_implementer_scanner_for(_backend()) is None

    def test_fleet_on_at_full_budget_builds_a_heartbeat_only_scanner(self) -> None:
        # Fleet-safety Stage 2: at full budget the scanner is STILL emitted when the
        # kill-switch is on (so the per-tick heartbeat runs), but claims nothing new.
        ImplementedIssueMarkerFactory(overlay="acme")  # budget full
        with (
            patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)),
            patch("teatree.core.fleet.wire.fleet_claim_enabled", return_value=True),
        ):
            scanner = _issue_implementer_scanner_for(_backend())
        assert isinstance(scanner, IssueImplementerScanner)
        assert scanner.can_claim is False

    def test_fleet_on_with_budget_can_claim(self) -> None:
        with (
            patch(_PATCH_TARGET, return_value=_enabled()),
            patch("teatree.core.fleet.wire.fleet_claim_enabled", return_value=True),
        ):
            scanner = _issue_implementer_scanner_for(_backend())
        assert isinstance(scanner, IssueImplementerScanner)
        assert scanner.can_claim is True

    def test_abandoned_marker_does_not_consume_budget(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme", abandoned=True)
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)):
            assert _issue_implementer_scanner_for(_backend()) is not None

    def test_budget_is_overlay_scoped(self) -> None:
        ImplementedIssueMarkerFactory(overlay="other")
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)):
            assert _issue_implementer_scanner_for(_backend("acme")) is not None

    def test_hostless_backend_emits_no_scanner(self) -> None:
        backend = OverlayBackends(name="acme", hosts=(), messaging=None, ready_labels=())
        with patch(_PATCH_TARGET, return_value=_enabled()):
            assert _issue_implementer_scanner_for(backend) is None

    def test_domain_slice_empty_when_disabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert jobs_for_domain(Domain.ISSUE_IMPLEMENTER, _backend()) == []

    def test_domain_slice_emits_one_scanner_when_enabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_enabled()):
            jobs = jobs_for_domain(Domain.ISSUE_IMPLEMENTER, _backend())
        assert [job.scanner.name for job in jobs] == ["issue_implementer"]
        assert jobs[0].overlay == "acme"


class IssueImplementerRequireLabelWarningTests(TestCase):
    """``require_label`` + an EMPTY label is a safe but silent no-op — warn the operator.

    Pre-#3235 an empty label silently disabled the whole loop; that warning now
    belongs only to the operator who has explicitly opted the label filter back ON
    and then left the label unset. Without the flag the empty label is expected and
    must NOT warn — intake is by trusted author.
    """

    def test_require_label_with_empty_label_emits_no_scanner_and_warns(self) -> None:
        with (
            patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_require_label=True)),
            self.assertLogs("teatree.loop.scanner_factories", level="WARNING") as captured,
        ):
            scanner = _issue_implementer_scanner_for(_backend("acme"))
        assert scanner is None
        joined = "\n".join(captured.output)
        assert "issue_implementer_label is empty" in joined
        assert "acme" in joined

    def test_require_label_with_a_label_does_not_warn(self) -> None:
        with (
            patch(
                _PATCH_TARGET,
                return_value=_enabled(issue_implementer_label="auto-implement", issue_implementer_require_label=True),
            ),
            self.assertNoLogs("teatree.loop.scanner_factories", level="WARNING"),
        ):
            assert _issue_implementer_scanner_for(_backend()) is not None

    def test_empty_label_without_the_flag_does_not_warn(self) -> None:
        with (
            patch(_PATCH_TARGET, return_value=_enabled()),
            self.assertNoLogs("teatree.loop.scanner_factories", level="WARNING"),
        ):
            assert _issue_implementer_scanner_for(_backend()) is not None

    def test_disabled_loop_does_not_warn(self) -> None:
        with (
            patch(_PATCH_TARGET, return_value=_settings(issue_implementer_require_label=True)),
            self.assertNoLogs("teatree.loop.scanner_factories", level="WARNING"),
        ):
            assert _issue_implementer_scanner_for(_backend()) is None


class IssueImplementerMiniLoopTests(TestCase):
    """The mini-loop is the live-tick entry point — enabled→dispatch, disabled→inert (#1554)."""

    def setUp(self) -> None:
        patcher = patch("teatree.core.review.author_trust.repo_is_internal", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mini_loop_identity(self) -> None:
        assert MINI_LOOP.name == "issue_implementer"
        assert MINI_LOOP.off_live_tick is False

    def test_disabled_loop_is_inert(self) -> None:
        host = _authored_host("https://github.com/souliane/teatree/issues/100")
        with patch(_PATCH_TARGET, return_value=_settings()):
            jobs = MINI_LOOP.build_jobs(backends=[_backend_with_host(host)])
        assert jobs == []

    def test_no_backends_is_inert(self) -> None:
        with patch(_PATCH_TARGET, return_value=_enabled()):
            assert MINI_LOOP.build_jobs(backends=None) == []

    def test_enabled_loop_claims_unlabelled_trusted_issue_and_dispatches_to_orchestrator(self) -> None:
        url = "https://github.com/souliane/teatree/issues/100"
        host = _authored_host(url)
        with patch(_PATCH_TARGET, return_value=_enabled()):
            jobs = MINI_LOOP.build_jobs(backends=[_backend_with_host(host)])
        assert [job.scanner.name for job in jobs] == ["issue_implementer"]

        signals = [signal for job in jobs for signal in job.scanner.scan()]
        claimed = [s for s in signals if s.kind == "issue_implementer.claimed"]
        assert [s.payload["url"] for s in claimed] == [url]

        actions = dispatch(claimed)
        agent_zones = [a.zone for a in actions if a.kind == "agent"]
        assert agent_zones == ["t3:orchestrator"]
        assert any(a.kind == "statusline" and a.zone == "action_needed" for a in actions)

    def test_untrusted_author_never_reaches_dispatch(self) -> None:
        """End-to-end fail-closed: a stranger's issue produces no signal, no action, no task."""
        url = "https://github.com/souliane/teatree/issues/100"
        host = _authored_host(url, author="random-user")
        with patch(_PATCH_TARGET, return_value=_enabled()):
            jobs = MINI_LOOP.build_jobs(backends=[_backend_with_host(host)])

        signals = [signal for job in jobs for signal in job.scanner.scan()]

        assert signals == []
        assert persist_agent_actions(dispatch(signals)) == []
        assert not Task.objects.exists()

    def test_claimed_issue_persists_orchestrator_coding_task(self) -> None:
        """A claimed auto-implement issue must produce the orchestrator dispatch — a real Ticket + coding Task.

        Regression (#3100/#3213): the scanner claimed the issue (an
        ``ImplementedIssueMarker`` row was written) and ``dispatch`` emitted the
        ``t3:orchestrator`` agent action, but the emitted payload omitted
        ``auto_start`` — so the shared ``_handle_orchestrator`` persistence handler
        (which returns ``None`` unless ``auto_start is True``) silently dropped it.
        No ``Ticket``/``Task`` was ever created and the claim stranded. This asserts
        the WHOLE path scan → dispatch → persist yields the coding Task.
        """
        url = "https://github.com/souliane/teatree/issues/100"
        host = _authored_host(url)
        with patch(_PATCH_TARGET, return_value=_enabled()):
            jobs = MINI_LOOP.build_jobs(backends=[_backend_with_host(host)])
        signals = [signal for job in jobs for signal in job.scanner.scan()]
        claimed = [s for s in signals if s.kind == "issue_implementer.claimed"]

        created = persist_agent_actions(dispatch(claimed))

        assert len(created) == 1
        task = created[0]
        assert task.phase == "coding"
        assert task.ticket.role == Ticket.Role.AUTHOR
        assert task.ticket.issue_url == url

    def test_claimed_issue_dispatch_never_double_dispatches(self) -> None:
        """Re-persisting the same claimed-issue dispatch is a no-op (idempotency)."""
        url = "https://github.com/souliane/teatree/issues/100"
        host = _authored_host(url)
        with patch(_PATCH_TARGET, return_value=_enabled()):
            jobs = MINI_LOOP.build_jobs(backends=[_backend_with_host(host)])
        claimed = [s for job in jobs for s in job.scanner.scan() if s.kind == "issue_implementer.claimed"]
        actions = dispatch(claimed)

        first = persist_agent_actions(actions)
        second = persist_agent_actions(actions)

        assert len(first) == 1
        assert second == []
        assert Task.objects.filter(ticket__issue_url=url, phase="coding").count() == 1
