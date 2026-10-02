"""The issue-disposition tick-job builder is scoped to the canonical core overlay, and ships off.

``_issue_disposition_scanner_for`` returns a scanner ONLY for
:data:`~teatree.loop.job_identity.CANONICAL_CORE_OVERLAY`; otherwise ``None`` (no job
emitted). That scope condition is the whole of the refusal — closing an issue is a
judgement about a backlog and this loop may make it only about repos we own — so the
core backend still emitting one is the control proving it is not a blanket refusal. The
whole builder also waits for ``auto_disposition_enabled``, so these cases opt in first.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import NEEDS_TRIAGE_LABEL, ConfigSetting, Ticket
from teatree.loop.dispatch import dispatch
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import CANONICAL_CORE_OVERLAY, Domain
from teatree.loop.scanner_factories import _issue_disposition_scanner_for
from teatree.loop.scanners.issue_disposition import CLOSE_CANDIDATE_KIND, IssueDispositionScanner

_DEAD_URL = "https://github.com/souliane/teatree/issues/700"
_FOREIGN_URL = "https://github.com/other-owner/other-repo/issues/701"


def _backend(name: str = CANONICAL_CORE_OVERLAY) -> OverlayBackends:
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


def _backend_with_host(host: CodeHostBackend, name: str = CANONICAL_CORE_OVERLAY) -> OverlayBackends:
    return OverlayBackends(name=name, hosts=(host,), messaging=None, ready_labels=(), identities=("alice",))


def _owned_backend_with_host(host: CodeHostBackend) -> OverlayBackends:
    overlay = MagicMock()
    overlay.review.merge_candidate_repo_slugs.return_value = ("souliane/teatree",)
    overlay.metadata.get_followup_repos.return_value = []
    overlay.get_workspace_repos.return_value = ["souliane/teatree"]
    return OverlayBackends(
        name=CANONICAL_CORE_OVERLAY,
        hosts=(host,),
        messaging=None,
        ready_labels=(),
        identities=("alice",),
        overlay=overlay,
    )


class _OptedIn(TestCase):
    """This file exercises the scanner itself, so each case opts the box in first."""

    def setUp(self) -> None:
        super().setUp()
        ConfigSetting.objects.set_value("auto_disposition_enabled", value=True)


class IssueDispositionScannerWiring(_OptedIn):
    def test_the_core_backend_builds_the_scanner(self) -> None:
        scanner = _issue_disposition_scanner_for(_backend())
        assert isinstance(scanner, IssueDispositionScanner)
        assert scanner.overlay_name == CANONICAL_CORE_OVERLAY
        assert scanner.identities == ("alice",)
        assert scanner.max_closes_per_tick == 5

    def test_hostless_backend_emits_no_scanner(self) -> None:
        backend = OverlayBackends(name=CANONICAL_CORE_OVERLAY, hosts=(), messaging=None, ready_labels=())
        assert _issue_disposition_scanner_for(backend) is None

    def test_the_domain_slice_emits_exactly_one_scanner(self) -> None:
        jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend())
        assert [job.scanner.name for job in jobs] == ["issue_disposition"]
        assert jobs[0].overlay == CANONICAL_CORE_OVERLAY


class IssueDispositionIsScopedToOwnRepos(_OptedIn):
    """Closing an issue is a judgement about a backlog, and this loop may only judge our own.

    The rule is posture-independent — "never on shared repos" is as true in ``present`` as
    under an egress forbid — so it is a condition at the scanner factory rather than an
    egress opinion. These assert it with the flag ON, so the scope refusal cannot be
    mistaken for the default-OFF gate doing the work.
    """

    def test_a_non_core_backend_emits_no_scanner(self) -> None:
        assert _issue_disposition_scanner_for(_backend("acme")) is None

    def test_a_non_core_backend_contributes_no_job_to_the_domain_slice(self) -> None:
        assert jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend("acme")) == []

    def test_a_non_core_backend_closes_nothing_even_for_a_genuinely_dead_issue(self) -> None:
        Ticket.objects.create(issue_url=_DEAD_URL, state=Ticket.State.DELIVERED)
        host = _dead_host()
        jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend_with_host(host, "acme"))
        assert [signal for job in jobs for signal in job.scanner.scan()] == []

    def test_the_core_backend_still_emits_one_so_the_scope_is_not_a_blanket_refusal(self) -> None:
        jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _backend())
        assert [job.overlay for job in jobs] == [CANONICAL_CORE_OVERLAY]

    def test_an_assigned_needs_triage_issue_in_a_foreign_repo_is_not_returned(self) -> None:
        Ticket.objects.create(issue_url=_FOREIGN_URL, state=Ticket.State.DELIVERED)
        host = _dead_host()
        foreign_issue: dict[str, object] = {
            "web_url": _FOREIGN_URL,
            "title": "someone else's shipped issue",
            "labels": [NEEDS_TRIAGE_LABEL],
            "state": "open",
        }

        def assigned_issues(*, assignee: str, repo_slugs: tuple[str, ...] = ()) -> list[dict[str, object]]:
            _ = assignee
            return [] if repo_slugs else [foreign_issue]

        host.list_assigned_issues.side_effect = assigned_issues

        jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _owned_backend_with_host(host))

        assert [signal for job in jobs for signal in job.scanner.scan()] == []
        host.list_assigned_issues.assert_called_once_with(
            assignee="alice",
            repo_slugs=("souliane/teatree",),
        )


class IssueDispositionClosesGenuinelyDeadIssues(_OptedIn):
    """The core backend emits a close candidate for a genuinely dead issue, and it routes."""

    def setUp(self) -> None:
        super().setUp()
        Ticket.objects.create(issue_url=_DEAD_URL, state=Ticket.State.DELIVERED)

    def test_a_dead_issue_emits_a_close_candidate_that_routes_to_mechanical(self) -> None:
        host = _dead_host()
        jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, _owned_backend_with_host(host))
        signals = [signal for job in jobs for signal in job.scanner.scan()]
        assert [s.kind for s in signals] == [CLOSE_CANDIDATE_KIND]

        actions = dispatch(signals)
        assert [(a.kind, a.zone) for a in actions] == [("mechanical", "close_dead_issue")]


class ObsoleteEvidenceIsReadFromTheCandidatesOwnClone(_OptedIn):
    """A referenced path is only evidence about the repo the issue lives in.

    The owned listing spans several repos, so a file missing from ANOTHER repo's clone says
    nothing about this issue, and a repo with no local clone at all cannot be judged — it
    must keep the issue open rather than read every referenced path as gone.
    """

    A_URL_SLUG = "souliane/repo-a"
    B_SLUG = "souliane/repo-b"
    B_URL = "https://github.com/souliane/repo-b/issues/30"

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="disposition_clones_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        patcher = patch("teatree.loop.scanner_factories.clone_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _clone(self, slug: str, *files: str) -> None:
        clone = self.root / slug
        (clone / ".git").mkdir(parents=True)
        for relative in files:
            (clone / relative).parent.mkdir(parents=True, exist_ok=True)
            (clone / relative).write_text("", encoding="utf-8")

    def _signals(self, referenced: str) -> list[object]:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = "alice"
        host.search_open_issues.return_value = []
        host.repo_for_issue_url.side_effect = lambda url: "/".join(url.split("/")[3:5])
        host.list_assigned_issues.return_value = [
            {
                "web_url": self.B_URL,
                "title": "Tidy the parser",
                "body": f"The helper in `{referenced}` is dead.",
                "labels": [NEEDS_TRIAGE_LABEL],
                "state": "open",
            }
        ]
        overlay = MagicMock()
        overlay.review.merge_candidate_repo_slugs.return_value = (self.A_URL_SLUG, self.B_SLUG)
        overlay.metadata.get_followup_repos.return_value = []
        overlay.get_workspace_repos.return_value = [self.A_URL_SLUG, self.B_SLUG]
        backend = OverlayBackends(
            name=CANONICAL_CORE_OVERLAY,
            hosts=(host,),
            messaging=None,
            ready_labels=(),
            identities=("alice",),
            overlay=overlay,
        )
        jobs = jobs_for_domain(Domain.ISSUE_DISPOSITION, backend)
        return [signal.payload.get("reason") for job in jobs for signal in job.scanner.scan()]

    def test_an_issue_in_a_repo_with_no_local_clone_stays_open(self) -> None:
        self._clone(self.A_URL_SLUG)

        assert self._signals("src/parser/helpers.py") == []

    def test_a_path_present_only_in_another_repo_does_not_keep_the_issue_open(self) -> None:
        self._clone(self.A_URL_SLUG, "src/parser/helpers.py")
        self._clone(self.B_SLUG)

        assert self._signals("src/parser/helpers.py") == ["obsolete"]

    def test_a_path_present_in_its_own_clone_keeps_the_issue_open(self) -> None:
        self._clone(self.A_URL_SLUG)
        self._clone(self.B_SLUG, "src/parser/helpers.py")

        assert self._signals("src/parser/helpers.py") == []


class IssueDispositionShipsOff(TestCase):
    """Closing issues is the owner's call, so the core backend builds nothing until opted in."""

    def test_the_core_backend_builds_no_scanner_by_default(self) -> None:
        assert _issue_disposition_scanner_for(_backend()) is None
