"""``workspace teardown`` must not reclaim a ticket whose PR/MR is still open.

Teardown is TICKET-scoped by design and reclaims every worktree of the resolved
ticket. Its ELIGIBILITY condition is whether the ticket is done, judged against
the forge: a sibling worktree whose branch backs a still-open MR must never be
reclaimed as collateral of a done-looking ticket.

Anti-vacuity coverage, driven through the real ``call_command("workspace",
"teardown", ...)`` against real git repos under a temp dir. Only the two
unstoppable externals are faked: the ``glab``/``gh`` subprocess and the forge
API behind ``get_pr_open_state``.

* ``TestRecordedPullRequestRows`` — the ticket-level view. An OPEN row refuses;
    a MERGED row proceeds; a row the forge reports CLOSED-unmerged proceeds even
    though ``PullRequest.State`` has no CLOSED member to record that with.
* ``TestUnrecordedMrBackingAWorktreeBranch`` — the incident itself. With ZERO
    recorded rows the forge is still asked whether the branch under teardown
    backs an open MR, so an MR the model never heard of still refuses. RED
    without the branch leg: a recorded-rows-only gate sees nothing and reclaims.
* ``TestFailsClosed`` — an unreadable forge refuses rather than reading
    "unknown" as "not open".
* ``TestOverride`` — ``--allow-open-prs`` is the escape, and it is NOT
    ``--force`` (which only waives the unpushed-commit guard).
* ``TestNoForgeRemote`` — a repo with no forge origin is CLEAR, not
    inconclusive: no forge, therefore no MR to protect.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

import teatree.core.gates.open_pr_teardown_gate as gate_mod
import teatree.core.management.commands._workspace.forge_pr_state as forge_state_mod
import teatree.core.management.commands.workspace as workspace_mod
from teatree.core.backend_protocols import PrOpenState
from teatree.core.gates.open_pr_teardown_gate import OpenPullRequestTeardownError, check_no_open_prs, open_pr_blockers
from teatree.core.management.commands._workspace.forge_pr_state import read_live_pr_state
from teatree.core.models import PullRequest, Ticket, Worktree
from teatree.core.runners import RunnerResult

_SLUG = "acme-org/backend"
_MR_URL = f"https://gitlab.com/{_SLUG}/-/merge_requests/7853"
_OPEN_BRANCH = "1701-drop-serialized-rollback"
_DONE_BRANCH = "1701-drop-stale-lint-ignores"


_GIT = shutil.which("git") or "git"


def _git(repo: Path, *args: str) -> None:
    subprocess.run([_GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_clone(root: Path, *, remote: str) -> Path:
    """A real main clone with one commit, optionally carrying a forge ``origin``."""
    clone = root / "clone"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "master")
    _git(clone, "config", "user.email", "dev@example.com")
    _git(clone, "config", "user.name", "dev")
    (clone / "README.md").write_text("seed\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-qm", "seed")
    if remote:
        _git(clone, "remote", "add", "origin", remote)
    return clone


class _TeardownHarness(TestCase):
    """A ticket with two sibling worktrees — real linked git worktrees of one clone."""

    remote = f"git@gitlab.com:{_SLUG}.git"  # privacy-scan:allow

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(forge_state_mod, "get_overlay_for_url", return_value=MagicMock()))
        self.ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/acme-org/backend/-/work_items/1701",
            state=Ticket.State.SHIPPED,
        )
        self.clone = _make_clone(self.tmp, remote=self.remote)
        self.done_dir = self._add_worktree("backend-lint-debt", _DONE_BRANCH)
        self.open_dir = self._add_worktree("backend-serialized-rollback", _OPEN_BRANCH)
        self.done_wt = self._worktree(self.done_dir, _DONE_BRANCH)
        self.open_wt = self._worktree(self.open_dir, _OPEN_BRANCH)
        self.runner = MagicMock()
        self.runner.run.return_value = RunnerResult(ok=True, detail="torn down")

    def _add_worktree(self, name: str, branch: str) -> Path:
        path = self.tmp / name
        _git(self.clone, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def _worktree(self, path: Path, branch: str) -> Worktree:
        return Worktree.objects.create(
            overlay="test",
            ticket=self.ticket,
            repo_path=path.name,
            branch=branch,
            extra={"worktree_path": str(path)},
            state=Worktree.State.PROVISIONED,
        )

    def _forge_cli(self, *, open_branches: set[str] | None = None, returncode: int = 0):
        """Fake the ``glab``/``gh`` probe: an open MR for every branch in *open_branches*."""
        opened = open_branches or set()

        def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            branch = cmd[cmd.index("--source-branch") + 1] if "--source-branch" in cmd else ""
            payload = [{"web_url": _MR_URL}] if branch in opened else []
            return subprocess.CompletedProcess(cmd, returncode, json.dumps(payload), "")

        return patch.object(gate_mod, "run_allowed_to_fail", side_effect=_run)

    def _forge_api(self, state: PrOpenState):
        """Fake the code host behind the injected reader, for the recorded-row leg."""
        host = MagicMock()
        host.get_pr_open_state.return_value = state
        return patch.object(forge_state_mod, "get_code_host_for_url", return_value=host)

    def _teardown(self, *extra_args: str) -> None:
        with patch.object(workspace_mod, "WorktreeTeardownRunner", return_value=self.runner):
            call_command("workspace", "teardown", *extra_args, path=str(self.done_dir))

    def assert_nothing_reclaimed(self) -> None:
        assert self.runner.run.call_count == 0, "the gate must refuse BEFORE any worktree is touched"
        assert Worktree.objects.filter(ticket=self.ticket).count() == 2
        assert Worktree.objects.get(pk=self.open_wt.pk).state == Worktree.State.PROVISIONED

    def assert_all_reclaimed(self) -> None:
        assert self.runner.run.call_count == 2, "both sibling worktrees should tear down"


class TestRecordedPullRequestRows(_TeardownHarness):
    def _record(self, state: str = PullRequest.State.OPEN) -> PullRequest:
        return PullRequest.objects.create(
            ticket=self.ticket,
            overlay="test",
            url=_MR_URL,
            repo="acme-org/backend",
            iid="7853",
            state=state,
        )

    def test_refuses_while_a_recorded_mr_is_open_on_the_forge(self) -> None:
        self._record()
        with self._forge_cli(), self._forge_api(PrOpenState.OPEN), pytest.raises(OpenPullRequestTeardownError) as exc:
            self._teardown()
        assert _MR_URL in str(exc.value)
        assert "--allow-open-prs" in str(exc.value)
        self.assert_nothing_reclaimed()

    def test_proceeds_when_the_recorded_mr_is_merged(self) -> None:
        self._record()
        with self._forge_cli(), self._forge_api(PrOpenState.MERGED):
            self._teardown()
        self.assert_all_reclaimed()

    def test_proceeds_when_the_recorded_mr_was_closed_unmerged(self) -> None:
        """``PullRequest.State`` cannot record CLOSED, so the model alone would block forever."""
        self._record()
        with self._forge_cli(), self._forge_api(PrOpenState.CLOSED):
            self._teardown()
        self.assert_all_reclaimed()

    def test_a_merged_row_is_settled_without_a_forge_call(self) -> None:
        self._record(state=PullRequest.State.MERGED)
        with self._forge_cli(), self._forge_api(PrOpenState.OPEN) as host_factory:
            self._teardown()
        assert host_factory.call_count == 0, "MERGED is terminal — no forge read needed"
        self.assert_all_reclaimed()


class TestUnrecordedMrBackingAWorktreeBranch(_TeardownHarness):
    """The incident: the open MR was recorded NOWHERE, so only the forge could see it."""

    def test_refuses_when_a_sibling_branch_backs_an_open_mr(self) -> None:
        assert not PullRequest.objects.filter(ticket=self.ticket).exists()
        with self._forge_cli(open_branches={_OPEN_BRANCH}), pytest.raises(OpenPullRequestTeardownError) as exc:
            self._teardown()
        assert _OPEN_BRANCH in str(exc.value)
        assert _MR_URL in str(exc.value)
        self.assert_nothing_reclaimed()

    def test_proceeds_when_no_branch_backs_an_open_mr(self) -> None:
        with self._forge_cli(open_branches=set()):
            self._teardown()
        self.assert_all_reclaimed()


class TestFailsClosed(_TeardownHarness):
    def test_refuses_when_the_branch_probe_fails(self) -> None:
        with self._forge_cli(returncode=1), pytest.raises(OpenPullRequestTeardownError) as exc:
            self._teardown()
        assert "unknown" in str(exc.value)
        self.assert_nothing_reclaimed()

    def test_refuses_when_the_forge_cli_is_missing(self) -> None:
        with (
            patch.object(gate_mod, "run_allowed_to_fail", side_effect=FileNotFoundError("glab")),
            pytest.raises(OpenPullRequestTeardownError),
        ):
            self._teardown()
        self.assert_nothing_reclaimed()

    def test_refuses_when_a_recorded_row_reads_unknown(self) -> None:
        PullRequest.objects.create(ticket=self.ticket, overlay="test", url=_MR_URL, repo="acme-org/backend", iid="7853")
        with self._forge_cli(), self._forge_api(PrOpenState.UNKNOWN), pytest.raises(OpenPullRequestTeardownError):
            self._teardown()
        self.assert_nothing_reclaimed()


class TestOverride(_TeardownHarness):
    def test_allow_open_prs_reclaims_despite_an_open_mr(self) -> None:
        with self._forge_cli(open_branches={_OPEN_BRANCH}):
            self._teardown("--allow-open-prs")
        self.assert_all_reclaimed()

    def test_force_alone_does_not_waive_the_open_pr_gate(self) -> None:
        """``--force`` waives the unpushed-commit guard only — it must not disable this gate."""
        with self._forge_cli(open_branches={_OPEN_BRANCH}), pytest.raises(OpenPullRequestTeardownError):
            self._teardown("--force")
        self.assert_nothing_reclaimed()


class TestNoForgeRemote(_TeardownHarness):
    remote = ""

    def test_a_repo_with_no_forge_origin_is_clear(self) -> None:
        with patch.object(gate_mod, "run_allowed_to_fail", side_effect=AssertionError("must not probe")):
            self._teardown()
        self.assert_all_reclaimed()


class TestGateHelpersDirect(_TeardownHarness):
    """The gate's public helpers, called directly (not only via the command)."""

    def test_open_pr_blockers_is_empty_for_no_worktrees(self) -> None:
        def _reader(_url: str) -> PrOpenState:
            return pytest.fail("must not read the forge when there is nothing to reclaim")

        assert open_pr_blockers(self.ticket, [], read_pr_state=_reader) == []

    def test_check_no_open_prs_override_skips_the_forge_and_never_raises(self) -> None:
        def _reader(_url: str) -> PrOpenState:
            return pytest.fail("the override must skip every forge read")

        # allow_open_prs short-circuits: no read, no raise.
        check_no_open_prs(self.ticket, [self.open_wt], read_pr_state=_reader, allow_open_prs=True)

    def test_read_live_pr_state_is_unknown_when_no_host_resolves(self) -> None:
        with patch.object(forge_state_mod, "get_code_host_for_url", return_value=None):
            assert read_live_pr_state(_MR_URL) == PrOpenState.UNKNOWN

    def test_read_live_pr_state_returns_the_hosts_reported_state(self) -> None:
        host = MagicMock()
        host.get_pr_open_state.return_value = PrOpenState.MERGED
        with patch.object(forge_state_mod, "get_code_host_for_url", return_value=host):
            assert read_live_pr_state(_MR_URL) == PrOpenState.MERGED
        host.get_pr_open_state.assert_called_once_with(pr_url=_MR_URL)

    def test_live_pr_state_is_unknown_for_a_blank_url(self) -> None:
        def _reader(_url: str) -> PrOpenState:
            return pytest.fail("a blank url must never be read")

        assert gate_mod._live_pr_state("", _reader) == PrOpenState.UNKNOWN

    def test_live_pr_state_fails_closed_when_the_reader_raises(self) -> None:
        def _reader(_url: str) -> PrOpenState:
            msg = "forge down"
            raise RuntimeError(msg)

        assert gate_mod._live_pr_state(_MR_URL, _reader) == PrOpenState.UNKNOWN

    def test_branch_pr_blockers_skips_a_worktree_with_no_dir_on_disk(self) -> None:
        gone = Worktree.objects.create(
            overlay="test",
            ticket=self.ticket,
            repo_path="backend",
            branch="1701-vanished",
            extra={"worktree_path": str(self.tmp / "does-not-exist")},
            state=Worktree.State.PROVISIONED,
        )
        assert gate_mod._branch_pr_blockers([gone]) == []

    def test_open_pr_url_for_branch_is_none_for_a_blank_branch(self) -> None:
        assert gate_mod._open_pr_url_for_branch(self.clone, "") is None

    def test_open_pr_url_for_branch_uses_the_github_probe_on_a_github_remote(self) -> None:
        with (
            patch.object(gate_mod.git, "remote_url", return_value="https://github.com/acme/backend"),
            patch.object(gate_mod, "_first_url", return_value="") as first_url,
        ):
            assert gate_mod._open_pr_url_for_branch(self.clone, "feature") == ""
        assert first_url.call_args.args[0][0] == "gh"

    def test_first_url_is_none_on_unparseable_json(self) -> None:
        result = subprocess.CompletedProcess(["glab"], 0, "not json at all", "")
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=result):
            assert gate_mod._first_url(["glab"], self.clone, key="web_url") is None

    def test_first_url_is_none_when_payload_is_not_a_list(self) -> None:
        result = subprocess.CompletedProcess(["glab"], 0, '{"web_url": "x"}', "")
        with patch.object(gate_mod, "run_allowed_to_fail", return_value=result):
            assert gate_mod._first_url(["glab"], self.clone, key="web_url") is None
