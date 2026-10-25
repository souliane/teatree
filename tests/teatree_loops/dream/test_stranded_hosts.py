from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Ticket
from teatree.core.models.dream_gap_ledger import pending_entries
from teatree.core.models.transition import TicketTransition
from teatree.loops.dream.stranded_hosts import release_stranded_hosts
from teatree.utils.git_branch import RefPresence, RemoteRefListing
from tests.factories import PullRequestFactory

UMBRELLA = "https://github.com/o/r/issues/249"


def _old_host(*, issue_url: str, keys: tuple[str, ...] = ("g1",), branch: str = "") -> Ticket:
    extra: dict[str, object] = {"dream_gap_batch": [{"gap_key": key, "cluster_key": key} for key in keys]}
    if branch:
        extra["branch"] = branch
    host = Ticket.objects.create(issue_url=issue_url, state=Ticket.State.WORK_STARTED, extra=extra)
    TicketTransition.objects.create(ticket=host, from_state="not_started", to_state="work_started")
    TicketTransition.objects.filter(ticket=host).update(created_at=timezone.now() - timedelta(days=30))
    return host


class TestRecordedBranch(TestCase):
    def setUp(self) -> None:
        self.umbrella = Ticket.objects.create(issue_url=UMBRELLA)
        patcher = patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=UMBRELLA)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_branch_absent_everywhere_releases_with_proof(self) -> None:
        host = _old_host(issue_url=f"{UMBRELLA}#dream-batch=old", branch="old-branch")
        with (
            patch("teatree.loops.dream.stranded_hosts.unambiguous_clone_path", return_value=Path("/repo")),
            patch(
                "teatree.loops.dream.stranded_hosts.list_remote_refs",
                return_value=RemoteRefListing("/repo", frozenset()),
            ),
            patch("teatree.loops.dream.stranded_hosts.ref_presence", return_value=RefPresence.ABSENT),
        ):
            report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert report.released_hosts == 1
        assert host.state == Ticket.State.IGNORED
        assert "absent" in host.extra["dream_gap_released"]["reason"]
        assert [entry["gap_key"] for entry in pending_entries(self.umbrella)] == ["g1"]

    def test_a_failed_probe_holds_the_host(self) -> None:
        host = _old_host(issue_url=f"{UMBRELLA}#dream-batch=old", branch="old-branch")
        with (
            patch("teatree.loops.dream.stranded_hosts.unambiguous_clone_path", return_value=Path("/repo")),
            patch("teatree.loops.dream.stranded_hosts.list_remote_refs", return_value=RemoteRefListing("/repo", None)),
            patch("teatree.loops.dream.stranded_hosts.ref_presence", return_value=RefPresence.UNKNOWN),
        ):
            report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert report.released_hosts == 0
        assert host.state == Ticket.State.WORK_STARTED
        assert pending_entries(self.umbrella) == []

    def test_remote_listing_is_bounded_to_one_call_per_pass(self) -> None:
        for number in range(2):
            _old_host(issue_url=f"{UMBRELLA}#dream-batch={number}", branch=f"old-{number}")
        with (
            patch("teatree.loops.dream.stranded_hosts.unambiguous_clone_path", return_value=Path("/repo")),
            patch(
                "teatree.loops.dream.stranded_hosts.list_remote_refs",
                return_value=RemoteRefListing("/repo", frozenset()),
            ) as listing,
            patch("teatree.loops.dream.stranded_hosts.ref_presence", return_value=RefPresence.ABSENT),
        ):
            report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert report.released_hosts == 2
        assert listing.call_count == 1


class TestRealIssueHost(TestCase):
    def setUp(self) -> None:
        self.umbrella = Ticket.objects.create(issue_url=UMBRELLA)
        patcher = patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=UMBRELLA)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_pull_request_row_holds_the_real_issue(self) -> None:
        host = _old_host(issue_url="https://github.com/o/r/issues/56")
        PullRequestFactory(ticket=host)

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert report.released_hosts == 0
        assert host.extra["dream_gap_batch"] == [{"gap_key": "g1", "cluster_key": "g1"}]
        assert pending_entries(self.umbrella) == []

    def test_a_host_without_recorded_activity_is_not_declared_stranded(self) -> None:
        host = Ticket.objects.create(
            issue_url="https://github.com/o/r/issues/56",
            state=Ticket.State.WORK_STARTED,
            extra={"dream_gap_batch": [{"gap_key": "g1", "cluster_key": "g1"}]},
        )

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert report.released_hosts == 0
        assert host.extra["dream_gap_batch"] == [{"gap_key": "g1", "cluster_key": "g1"}]

    def test_pr_opened_state_without_a_row_holds_the_real_issue(self) -> None:
        host = _old_host(issue_url="https://github.com/o/r/issues/56")
        host.state = Ticket.State.PR_OPENED
        host.save(update_fields=["state"])

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert report.released_hosts == 0
        assert host.extra["dream_gap_batch"] == [{"gap_key": "g1", "cluster_key": "g1"}]

    def test_only_undispositioned_gaps_return_from_a_real_issue(self) -> None:
        host = _old_host(issue_url="https://github.com/o/r/issues/56", keys=("addressed", "misfit"))
        host.merge_extra(
            merge_into_dicts={
                "dream_gap_dispositions": {"addressed": {"disposition": "address", "evidence": "merged fix"}}
            }
        )

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        self.umbrella.refresh_from_db()
        assert (report.released_hosts, report.returned_gaps) == (1, 1)
        assert host.state == Ticket.State.WORK_STARTED
        assert host.extra["dream_gap_batch"] == [{"gap_key": "addressed", "cluster_key": "addressed"}]
        assert [entry["gap_key"] for entry in pending_entries(self.umbrella)] == ["misfit"]
