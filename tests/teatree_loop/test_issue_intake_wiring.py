"""The unified intake tick-job builder is gated by the default-OFF triple gate (#3634).

``_issue_intake_scanner_for`` returns a scanner ONLY when the loop is opted in for
the overlay AND the in-flight concurrency budget has room; otherwise ``None`` (no
job emitted), so with the default-OFF config the domain slice is empty.

The mini-loop wires it into the live tick and routes the emitted
``issue_intake.admitted`` signal to ``t3:orchestrator`` (maker-side kickoff).
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
from teatree.loop.scanner_factories import _issue_intake_scanner_for
from teatree.loop.scanners.issue_intake import IssueIntakeScanner
from teatree.loops.issue_implementer.loop import MINI_LOOP
from tests.factories import ImplementedIssueMarkerFactory, TicketFactory

_PATCH_TARGET = "teatree.loop.scanner_factories._effective_settings_for_overlay"


def _backend(name: str = "acme", overlay: object = None) -> OverlayBackends:
    return OverlayBackends(
        name=name,
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=None,
        ready_labels=(),
        identities=("alice",),
        overlay=overlay,
    )


def _overlay_with_repos(*, followup: list[str], merge_candidates: list[str] | None = None) -> MagicMock:
    """A minimal overlay stub exposing the repo-slug hooks the factory resolves."""
    overlay = MagicMock()
    overlay.metadata.get_followup_repos.return_value = followup
    overlay.review.merge_candidate_repo_slugs.return_value = merge_candidates or []
    return overlay


def _authored_host(*urls: str, author: str = "alice") -> CodeHostBackend:
    """A host whose author-scoped issue query returns *urls*, all authored by *author*."""
    host = MagicMock(spec=CodeHostBackend)
    host.current_user.return_value = "alice"
    host.list_authored_issues.return_value = [
        {"web_url": url, "title": f"do {url}", "labels": [], "state": "open", "user": {"login": author}} for url in urls
    ]
    host.list_labeled_issues.return_value = []
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


class IssueIntakeGateTests(TestCase):
    def test_disabled_by_default_emits_no_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert _issue_intake_scanner_for(_backend()) is None

    def test_enabled_with_budget_builds_scanner(self) -> None:
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_label="auto-implement")):
            scanner = _issue_intake_scanner_for(_backend())
        assert isinstance(scanner, IssueIntakeScanner)
        assert scanner.admit_label == "auto-implement"
        assert scanner.overlay_name == "acme"
        assert scanner.identities == ("alice",)

    def test_unset_label_falls_back_to_the_shipped_admit_label(self) -> None:
        """An unset ``issue_implementer_label`` still recognises the shipped ``t3-auto`` convention."""
        with patch(_PATCH_TARGET, return_value=_enabled()):
            scanner = _issue_intake_scanner_for(_backend())
        assert isinstance(scanner, IssueIntakeScanner)
        assert scanner.admit_label == "t3-auto"
        assert scanner.can_claim is True

    def test_trusted_authors_are_resolved_from_the_config_union(self) -> None:
        """The builder hands the scanner the UNION of aliases + the ``trusted_issue_authors`` allowlist."""
        settings = _settings(
            issue_implementer_enabled=True,
            user_identity_aliases=["souliane"],
            trusted_issue_authors=["trusted-colleague"],
        )
        with patch(_PATCH_TARGET, return_value=settings):
            scanner = _issue_intake_scanner_for(_backend())
        assert isinstance(scanner, IssueIntakeScanner)
        assert set(scanner.trusted_authors) == {"souliane", "trusted-colleague"}

    def test_owned_repo_slugs_are_resolved_from_the_overlay(self) -> None:
        """The builder scopes intake to the overlay's own repos — the cross-repo firehose fix."""
        overlay = _overlay_with_repos(followup=["souliane/teatree"], merge_candidates=["souliane/teatree-e2e"])
        with patch(_PATCH_TARGET, return_value=_enabled()):
            scanner = _issue_intake_scanner_for(_backend(overlay=overlay))
        assert isinstance(scanner, IssueIntakeScanner)
        assert set(scanner.repo_slugs) == {"souliane/teatree", "souliane/teatree-e2e"}

    def test_no_overlay_leaves_repo_slugs_empty(self) -> None:
        """A backend with no overlay keeps intake unscoped (back-compat, no crash)."""
        with patch(_PATCH_TARGET, return_value=_enabled()):
            scanner = _issue_intake_scanner_for(_backend())
        assert isinstance(scanner, IssueIntakeScanner)
        assert scanner.repo_slugs == ()

    def test_concurrency_at_max_emits_no_scanner(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme")
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)):
            assert _issue_intake_scanner_for(_backend()) is None

    def test_fleet_on_at_full_budget_builds_a_heartbeat_only_scanner(self) -> None:
        # Fleet-safety Stage 2: at full budget the scanner is STILL emitted when the
        # kill-switch is on (so the per-tick heartbeat runs), but claims nothing new.
        ImplementedIssueMarkerFactory(overlay="acme")  # budget full
        with (
            patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)),
            patch("teatree.core.fleet.wire.fleet_claim_enabled", return_value=True),
        ):
            scanner = _issue_intake_scanner_for(_backend())
        assert isinstance(scanner, IssueIntakeScanner)
        assert scanner.can_claim is False

    def test_fleet_on_with_budget_can_claim(self) -> None:
        with (
            patch(_PATCH_TARGET, return_value=_enabled()),
            patch("teatree.core.fleet.wire.fleet_claim_enabled", return_value=True),
        ):
            scanner = _issue_intake_scanner_for(_backend())
        assert isinstance(scanner, IssueIntakeScanner)
        assert scanner.can_claim is True

    def test_orphaned_terminal_ticket_marker_is_reconciled_and_budget_frees(self) -> None:
        """#3275 jam: a dispatched marker whose ticket already merged strands the budget.

        Pre-fix the factory read a full ``in_flight_count`` and returned ``None``
        forever (intake permanently jammed). The tick-time reconcile releases the
        orphan to COMPLETED, so the budget frees and a scanner is built again.
        """
        url = "https://github.com/souliane/teatree/issues/42"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.MERGED)
        ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)  # DISPATCHED, jams budget=1
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)):
            scanner = _issue_intake_scanner_for(_backend())
        assert scanner is not None

    def test_abandoned_marker_does_not_consume_budget(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme", abandoned=True)
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)):
            assert _issue_intake_scanner_for(_backend()) is not None

    def test_budget_is_overlay_scoped(self) -> None:
        ImplementedIssueMarkerFactory(overlay="other")
        with patch(_PATCH_TARGET, return_value=_enabled(issue_implementer_max_concurrent=1)):
            assert _issue_intake_scanner_for(_backend("acme")) is not None

    def test_hostless_backend_emits_no_scanner(self) -> None:
        backend = OverlayBackends(name="acme", hosts=(), messaging=None, ready_labels=())
        with patch(_PATCH_TARGET, return_value=_enabled()):
            assert _issue_intake_scanner_for(backend) is None

    def test_domain_slice_empty_when_disabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_settings()):
            assert jobs_for_domain(Domain.ISSUE_IMPLEMENTER, _backend()) == []

    def test_domain_slice_emits_one_scanner_when_enabled(self) -> None:
        with patch(_PATCH_TARGET, return_value=_enabled()):
            jobs = jobs_for_domain(Domain.ISSUE_IMPLEMENTER, _backend())
        assert [job.scanner.name for job in jobs] == ["issue_intake"]
        assert jobs[0].overlay == "acme"


class IssueIntakeMiniLoopTests(TestCase):
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
        assert [job.scanner.name for job in jobs] == ["issue_intake"]

        signals = [signal for job in jobs for signal in job.scanner.scan()]
        claimed = [s for s in signals if s.kind == "issue_intake.admitted"]
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
        claimed = [s for s in signals if s.kind == "issue_intake.admitted"]

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
        claimed = [s for job in jobs for s in job.scanner.scan() if s.kind == "issue_intake.admitted"]
        actions = dispatch(claimed)

        first = persist_agent_actions(actions)
        second = persist_agent_actions(actions)

        assert len(first) == 1
        assert second == []
        assert Task.objects.filter(ticket__issue_url=url, phase="coding").count() == 1
