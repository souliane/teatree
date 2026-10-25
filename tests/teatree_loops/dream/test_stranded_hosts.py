import os
import tempfile
from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.config import UserSettings
from teatree.core.models import Ticket
from teatree.core.models.dream_gap_ledger import pending_entries
from teatree.core.models.transition import TicketTransition
from teatree.loops.dream.gap_coverage import gap_coverage
from teatree.loops.dream.stranded_hosts import RELEASE_HOSTS_PER_PASS, release_stranded_hosts, stranded_host_count
from teatree.utils import git_branch
from tests._git_repo import make_git_repo, run_git
from tests.factories import PullRequestFactory, SessionFactory, TaskFactory, WorktreeFactory

UMBRELLA = "https://github.com/o/r/issues/249"
REAL_ISSUE = "https://github.com/o/r/issues/56"


def _entries(*keys: str) -> list[dict[str, str]]:
    return [{"gap_key": key, "cluster_key": key} for key in keys]


def _shell_url(name: str) -> str:
    return f"{UMBRELLA}#dream-batch={name}"


def _old_host(
    issue_url: str,
    *,
    keys: tuple[str, ...] = ("g1",),
    idle_days: int = 30,
    branch: str = "",
    repos: tuple[str, ...] = (),
) -> Ticket:
    extra: dict[str, object] = {"dream_gap_batch": _entries(*keys)}
    if branch:
        extra["branch"] = branch
    host = Ticket.objects.create(issue_url=issue_url, state=Ticket.State.WORK_STARTED, extra=extra, repos=list(repos))
    TicketTransition.objects.create(ticket=host, from_state="not_started", to_state="work_started")
    TicketTransition.objects.filter(ticket=host).update(created_at=timezone.now() - timedelta(days=idle_days))
    return host


def _in_state(host: Ticket, state: str) -> Ticket:
    host.state = state
    host.save(update_fields=["state"])
    return host


class _StrandedCase(TestCase):
    def setUp(self) -> None:
        self.umbrella = Ticket.objects.create(issue_url=UMBRELLA)
        self.enterContext(patch("teatree.core.models.dream_gap_ledger.dream_umbrella_url", return_value=UMBRELLA))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"T3_WORKSPACE_DIR": str(self.root / "workspace")}))

    def clone(self, name: str = "o/r") -> Path:
        base = self.root / name.replace("/", "-")
        seed = make_git_repo(base / "seed")
        run_git(base, "clone", "-q", "--bare", str(seed), str(base / "origin.git"))
        clone = self.root / "workspace" / name
        clone.parent.mkdir(parents=True, exist_ok=True)
        run_git(base, "clone", "-q", str(base / "origin.git"), str(clone))
        return clone

    def pending_keys(self) -> list[str]:
        self.umbrella.refresh_from_db()
        return [str(entry["gap_key"]) for entry in pending_entries(self.umbrella)]


class TestSelection(_StrandedCase):
    def test_a_host_idle_for_fourteen_days_is_released_and_one_idle_for_thirteen_is_not(self) -> None:
        old = _old_host(_shell_url("old"), idle_days=14)
        fresh = _old_host(_shell_url("fresh"), keys=("g2",), idle_days=13)

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        old.refresh_from_db()
        fresh.refresh_from_db()
        assert (report.released_hosts, report.returned_gaps) == (1, 1)
        assert old.state == Ticket.State.IGNORED
        assert fresh.state == Ticket.State.WORK_STARTED
        assert self.pending_keys() == ["g1"]

    def test_a_host_with_a_live_task_is_not_stranded_and_stays_untouched(self) -> None:
        host = _old_host(_shell_url("busy"))
        TaskFactory(ticket=host, session__ticket=host)

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts, stranded_host_count()) == (0, 0, 0)
        assert host.state == Ticket.State.WORK_STARTED
        assert self.pending_keys() == []

    def test_one_pass_releases_the_largest_hosts_first_up_to_the_cap(self) -> None:
        sizes = range(1, RELEASE_HOSTS_PER_PASS + 2)
        hosts = {
            size: _old_host(_shell_url(f"h{size}"), keys=tuple(f"h{size}-{n}" for n in range(size))) for size in sizes
        }

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        hosts[1].refresh_from_db()
        assert report.released_hosts == RELEASE_HOSTS_PER_PASS
        assert report.returned_gaps == sum(range(2, RELEASE_HOSTS_PER_PASS + 2))
        assert hosts[1].state == Ticket.State.WORK_STARTED
        assert len(self.pending_keys()) == report.returned_gaps

    def test_a_held_host_does_not_take_a_slot_from_the_releasable_ones(self) -> None:
        held = _old_host(_shell_url("held"), keys=tuple(f"held-{n}" for n in range(40)))
        held.merge_extra(
            merge_into_dicts={"dream_gap_dispositions": {"held-0": {"disposition": "address", "evidence": "fix"}}}
        )
        for number in range(RELEASE_HOSTS_PER_PASS):
            _old_host(_shell_url(f"free{number}"), keys=(f"free-{number}",))

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (report.released_hosts, report.held_hosts) == (RELEASE_HOSTS_PER_PASS, 1)

    def test_a_second_pass_changes_nothing(self) -> None:
        _old_host(_shell_url("old"))
        release_stranded_hosts(umbrella_url=UMBRELLA)

        again = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (again.released_hosts, again.returned_gaps) == (0, 0)
        assert self.pending_keys() == ["g1"]

    def test_a_dry_run_counts_what_it_would_release_and_writes_nothing(self) -> None:
        host = _old_host(_shell_url("old"), keys=("g1", "g2"))

        report = release_stranded_hosts(umbrella_url=UMBRELLA, dry_run=True)

        host.refresh_from_db()
        assert (report.released_hosts, report.returned_gaps) == (1, 2)
        assert host.state == Ticket.State.WORK_STARTED
        assert [entry["gap_key"] for entry in host.extra["dream_gap_batch"]] == ["g1", "g2"]
        assert self.pending_keys() == []

    def test_a_dry_run_holds_a_host_the_ledger_would_refuse(self) -> None:
        host = _old_host(_shell_url("invalid-claim"))
        host.merge_extra(set_keys={"dream_gap_claimed_delivered": ["not-in-batch"]})

        report = release_stranded_hosts(umbrella_url=UMBRELLA, dry_run=True)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert report.held_reasons == ((host.pk, f"ticket {host.pk} claims keys outside its gap batch"),)
        assert host.state == Ticket.State.WORK_STARTED
        assert self.pending_keys() == []

    def test_the_stranded_count_follows_the_release(self) -> None:
        _old_host(_shell_url("a"))
        _old_host(_shell_url("b"), keys=("g2",))
        _old_host(_shell_url("fresh"), keys=("g3",), idle_days=1)
        assert stranded_host_count() == 2

        release_stranded_hosts(umbrella_url=UMBRELLA)

        assert stranded_host_count() == 0


class TestRecordedBranch(_StrandedCase):
    def release_reason(self, host: Ticket) -> str:
        host.refresh_from_db()
        return str(host.extra.get("dream_gap_released", {}).get("reason", ""))

    def test_a_branch_that_exists_nowhere_is_released_with_the_proof(self) -> None:
        self.clone()
        host = _old_host(_shell_url("old"), branch="gone")

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert report.released_hosts == 1
        assert "branch gone absent locally, in tracking, and on origin" in self.release_reason(host)
        assert self.pending_keys() == ["g1"]

    def test_a_branch_pushed_only_to_origin_with_commits_is_held(self) -> None:
        clone = self.clone()
        run_git(clone, "checkout", "-q", "-b", "remote-only")
        run_git(clone, "commit", "-q", "--allow-empty", "-m", "work")
        run_git(clone, "push", "-q", "origin", "remote-only")
        run_git(clone, "checkout", "-q", "main")
        run_git(clone, "branch", "-q", "-D", "remote-only")
        run_git(clone, "update-ref", "-d", "refs/remotes/origin/remote-only")
        host = _old_host(_shell_url("old"), branch="remote-only")

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert host.state == Ticket.State.WORK_STARTED

    def test_a_local_branch_with_a_unique_commit_is_held(self) -> None:
        clone = self.clone()
        run_git(clone, "checkout", "-q", "-b", "wip")
        run_git(clone, "commit", "-q", "--allow-empty", "-m", "local only")
        run_git(clone, "checkout", "-q", "main")
        host = _old_host(_shell_url("old"), branch="wip")

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert host.state == Ticket.State.WORK_STARTED

    def test_a_local_branch_already_on_the_default_branch_is_released_as_redundant(self) -> None:
        clone = self.clone()
        run_git(clone, "branch", "merged")
        host = _old_host(_shell_url("old"), branch="merged")

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert report.released_hosts == 1
        assert "branch merged redundant on origin/main" in self.release_reason(host)

    def test_an_unreachable_origin_holds_the_host(self) -> None:
        clone = self.clone()
        run_git(clone, "remote", "set-url", "origin", str(self.root / "missing.git"))
        host = _old_host(_shell_url("old"), branch="gone")

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert host.state == Ticket.State.WORK_STARTED

    def test_two_clones_of_the_same_name_hold_the_host(self) -> None:
        self.clone("a/r")
        self.clone("b/r")
        host = _old_host(_shell_url("old"), branch="gone", repos=("r",))

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)

    def test_two_stored_clone_paths_hold_the_host(self) -> None:
        first = self.clone("o/r")
        second = self.clone("o/s")
        host = _old_host(_shell_url("old"), branch="gone")
        for clone in (first, second):
            WorktreeFactory(
                ticket=host,
                branch="gone",
                extra={"clone_path": str(clone), "worktree_path": str(self.root / "missing")},
            )

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert self.pending_keys() == []

    def test_two_recorded_branch_names_hold_the_host(self) -> None:
        self.clone()
        host = _old_host(_shell_url("old"), branch="gone")
        WorktreeFactory(
            ticket=host,
            branch="different",
            extra={"worktree_path": str(self.root / "missing")},
        )

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert self.pending_keys() == []
        assert host.state == Ticket.State.WORK_STARTED

    def test_each_clone_is_listed_once_however_many_hosts_use_it(self) -> None:
        self.clone("o/r")
        self.clone("o/s")
        for number, repo in enumerate(("o/r", "o/s", "o/r", "o/s")):
            _old_host(_shell_url(f"h{number}"), keys=(f"g{number}",), branch=f"gone-{number}", repos=(repo,))

        with patch("teatree.loops.dream.stranded_hosts.list_remote_refs", wraps=git_branch.list_remote_refs) as listing:
            report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert report.released_hosts == 4
        assert sorted(call.args[0].rsplit("/", 1)[-1] for call in listing.call_args_list) == ["r", "s"]

    def test_a_listing_that_times_out_stops_every_further_remote_call(self) -> None:
        self.clone("o/r")
        self.clone("o/s")
        first = _old_host(_shell_url("first"), keys=("g1", "g2"), branch="gone-1", repos=("o/r",))
        second = _old_host(_shell_url("second"), keys=("g3",), branch="gone-2", repos=("o/s",))
        real = git_branch.run_with_status

        def hang_on_listing(
            *, repo: str, args: list[str], env: dict[str, str] | None = None, timeout: float | None = None
        ) -> CompletedProcess[str]:
            if "ls-remote" in args:
                raise TimeoutExpired(cmd=args, timeout=30)
            return real(repo=repo, args=args, env=env, timeout=timeout)

        with patch("teatree.utils.git_branch.run_with_status", side_effect=hang_on_listing) as probes:
            report = release_stranded_hosts(umbrella_url=UMBRELLA)

        first.refresh_from_db()
        second.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 2)
        assert (first.state, second.state) == (Ticket.State.WORK_STARTED, Ticket.State.WORK_STARTED)
        assert sum("ls-remote" in call.kwargs["args"] for call in probes.call_args_list) == 1


class TestHeldShells(_StrandedCase):
    def test_a_shell_with_recorded_dispositions_is_held(self) -> None:
        host = _old_host(_shell_url("old"), keys=("addressed", "open"))
        host.merge_extra(
            merge_into_dicts={"dream_gap_dispositions": {"addressed": {"disposition": "address", "evidence": "fix"}}}
        )

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert report.held_reasons == ((host.pk, "recorded dispositions"),)
        assert host.state == Ticket.State.WORK_STARTED
        assert self.pending_keys() == []

    def test_a_shell_with_a_pull_request_state_or_row_is_held(self) -> None:
        opened = _in_state(_old_host(_shell_url("opened")), Ticket.State.PR_OPENED)
        review_requested = _in_state(_old_host(_shell_url("review"), keys=("g2",)), Ticket.State.REVIEW_REQUESTED)
        with_row = _old_host(_shell_url("row"), keys=("g3",))
        PullRequestFactory(ticket=with_row)

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (report.released_hosts, report.held_hosts) == (0, 3)
        for host in (opened, review_requested, with_row):
            host.refresh_from_db()
            assert host.extra["dream_gap_batch"]
        assert self.pending_keys() == []

    def test_a_shell_with_a_live_worktree_is_held(self) -> None:
        host = _old_host(_shell_url("old"))
        WorktreeFactory(ticket=host, extra={"worktree_path": str(self.root)})

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert host.state == Ticket.State.WORK_STARTED

    def test_an_unverified_delivery_claim_does_not_hold_the_shell_and_the_stamp_keeps_it(self) -> None:
        host = _old_host(_shell_url("old"), keys=("g1", "g2"))
        host.merge_extra(set_keys={"dream_gap_claimed_delivered": ["g1"]})

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        stamp = host.extra["dream_gap_released"]
        assert (report.released_hosts, report.returned_gaps) == (1, 2)
        assert host.extra["dream_gap_claimed_delivered"] == []
        assert stamp["claimed_delivered"] == ["g1"]
        assert [entry["gap_key"] for entry in stamp["batch"]] == ["g1", "g2"]


class TestRealIssueHost(_StrandedCase):
    def test_a_dry_run_holds_a_third_decline_without_a_memory_row(self) -> None:
        host = _old_host(REAL_ISSUE)
        host.extra["dream_gap_batch"][0]["declined_hosts"] = [100, 101]
        host.save(update_fields=["extra"])

        report = release_stranded_hosts(umbrella_url=UMBRELLA, dry_run=True)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert "has no memory row to retain" in report.held_reasons[0][1]
        assert host.extra["dream_gap_batch"] == [{"gap_key": "g1", "cluster_key": "g1", "declined_hosts": [100, 101]}]
        assert self.pending_keys() == []

    def test_a_live_worktree_holds_the_real_issue(self) -> None:
        host = _old_host(REAL_ISSUE)
        WorktreeFactory(ticket=host, branch="", extra={"worktree_path": str(self.root)})

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert self.pending_keys() == []

    def test_a_live_session_holds_the_real_issue_when_liveness_is_unbounded(self) -> None:
        host = _old_host(REAL_ISSUE)
        session = SessionFactory(ticket=host)
        type(session).objects.filter(pk=session.pk).update(started_at=timezone.now() - timedelta(days=30))
        with patch(
            "teatree.core.managers_session.get_effective_settings",
            return_value=UserSettings(session_stale_after_hours=0),
        ):
            report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert self.pending_keys() == []

    def test_a_pull_request_row_holds_the_real_issue(self) -> None:
        host = _old_host(REAL_ISSUE)
        PullRequestFactory(ticket=host)

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.held_hosts) == (0, 1)
        assert host.extra["dream_gap_batch"] == _entries("g1")
        assert self.pending_keys() == []

    def test_a_state_at_or_after_pr_opened_holds_the_real_issue_without_a_row(self) -> None:
        opened = _in_state(_old_host(f"{REAL_ISSUE}1"), Ticket.State.PR_OPENED)
        review_requested = _in_state(_old_host(f"{REAL_ISSUE}2"), Ticket.State.REVIEW_REQUESTED)

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        assert (report.released_hosts, report.held_hosts) == (0, 2)
        for host in (opened, review_requested):
            host.refresh_from_db()
            assert host.extra["dream_gap_batch"] == _entries("g1")
        assert self.pending_keys() == []

    def test_a_host_without_recorded_activity_is_not_declared_stranded(self) -> None:
        host = Ticket.objects.create(
            issue_url=REAL_ISSUE, state=Ticket.State.WORK_STARTED, extra={"dream_gap_batch": _entries("g1")}
        )

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert report.released_hosts == 0
        assert host.extra["dream_gap_batch"] == _entries("g1")

    def test_only_undispositioned_gaps_return_and_the_host_keeps_its_state(self) -> None:
        host = _old_host(REAL_ISSUE, keys=("addressed", "misfit"))
        host.merge_extra(
            merge_into_dicts={"dream_gap_dispositions": {"addressed": {"disposition": "address", "evidence": "fix"}}}
        )

        report = release_stranded_hosts(umbrella_url=UMBRELLA)

        host.refresh_from_db()
        assert (report.released_hosts, report.returned_gaps) == (1, 1)
        assert host.state == Ticket.State.WORK_STARTED
        assert host.extra["dream_gap_batch"] == _entries("addressed")
        assert host.extra["dream_gap_dispositions"] == {"addressed": {"disposition": "address", "evidence": "fix"}}
        assert self.pending_keys() == ["misfit"]


class TestCoverageStaysFlat(_StrandedCase):
    def test_a_release_leaves_every_gap_coverage_list_as_it_was(self) -> None:
        _old_host(_shell_url("old"), keys=("g1", "g2"))
        _old_host(REAL_ISSUE, keys=("g3",))
        before = gap_coverage(umbrella_url=UMBRELLA)

        release_stranded_hosts(umbrella_url=UMBRELLA)

        after = gap_coverage(umbrella_url=UMBRELLA)
        assert before.ok
        assert after == before
