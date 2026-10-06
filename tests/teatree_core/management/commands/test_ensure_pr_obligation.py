"""An ``ensure-pr`` deferral must leave a durable, drainable obligation.

``ensure-pr`` runs in the git PRE-push hook. On a first push the branch is not
on the remote yet, so the PR create is deferred — and git has no client-side
post-push hook to re-run it, so the deferral had no drain at all: exit 0,
"Passed", nothing stored, branch shipped with no PR.

Real git under ``tmp_path`` (a bare origin plus a clone) so the deferral is the
genuine one — the branch is absent from the remote because it was never pushed,
not because a classifier was patched to say so.
"""

import shlex
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
import yaml
from django.core.management import call_command
from django.db import OperationalError, connection
from django.db.models.query import QuerySet
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.cli.doctor.checks_pending_pr import check_pending_pull_requests
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.gates import orphan_guard
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands import pr as pr_command
from teatree.core.models import PendingPullRequest
from teatree.core.models.pending_pull_request import MAX_DRAIN_ATTEMPTS
from teatree.utils.disposable_checkout import DISPOSABLE_ROOTS_ENV
from teatree.utils.run import CommandFailedError
from tests._process_liveness import is_gone_within
from tests.teatree_core.cleanup._shared import _run_git
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY

_LOCKED = OperationalError("database is locked")


def _first_push_repo(tmp_path: Path) -> tuple[Path, str]:
    """A clone whose ``feat/orphan`` branch carries work git has never pushed."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run_git("init", "--bare", "--initial-branch=main", cwd=origin)

    repo = tmp_path / "clone"
    repo.mkdir()
    _run_git("init", "--initial-branch=main", cwd=repo)
    _run_git("config", "user.email", "t@example.com", cwd=repo)
    _run_git("config", "user.name", "T", cwd=repo)
    (repo / "README.md").write_text("base\n")
    _run_git("add", "README.md", cwd=repo)
    _run_git("commit", "-m", "chore: base", cwd=repo)
    _run_git("remote", "add", "origin", str(origin), cwd=repo)
    _run_git("push", "-u", "origin", "main", cwd=repo)
    _run_git("remote", "set-head", "origin", "main", cwd=repo)

    _run_git("checkout", "-b", "feat/orphan", cwd=repo)
    (repo / "feature.py").write_text("value = 1\n")
    _run_git("add", "feature.py", cwd=repo)
    _run_git("commit", "-m", "feat: add the feature", cwd=repo)
    return repo, "feat/orphan"


class EnsurePrDeferralIsAnObligationTestCase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_first_push_deferral_records_a_pending_pull_request(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)

        result = cast("dict[str, object]", call_command("pr", "ensure-pr", repo=str(repo), branch=branch))

        assert "not on remote yet" in str(result["skipped"])
        assert result["owed"] is True
        owed = PendingPullRequest.objects.get(branch=branch)
        assert owed.repo_path == str(repo)
        assert owed.drain_attempts == 0

    def test_missing_branch_is_reported_without_creating_a_pr(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        _run_git("checkout", "main", cwd=repo)
        _run_git("branch", "-D", branch, cwd=repo)
        with patch.object(pr_command, "create_or_defer_pr", return_value={"url": "unexpected"}) as create_pr:
            result = cast(
                "dict[str, object]",
                call_command("pr", "ensure-pr", repo=str(repo), branch=branch),
            )

        create_pr.assert_not_called()
        assert result["branch"] == branch
        assert branch in str(result["skipped"])

    def test_re_deferring_the_same_branch_owes_once(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)

        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)
        call_command("pr", "ensure-pr", repo=str(repo), branch=branch)

        assert PendingPullRequest.objects.filter(branch=branch).count() == 1

    def test_pre_push_race_deferral_persists_the_computed_spec(self) -> None:
        """#792's stale-remote defer owes the PR spec ``create_or_defer_pr`` already built."""
        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.create_pr.side_effect = CommandFailedError(
            cmd=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr="GraphQL: No commits between main and feat-q (createPullRequest)",
        )
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(ensure_pr_mod.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(ensure_pr_mod, "_branch_own_commit_message", return_value=("feat: cool thing", "body")),
        ):
            result = ensure_pr_mod.create_or_defer_pr("/repo/path", "feat-q")

        assert "pre-push race" in str(result["skipped"])
        assert result["owed"] is True
        owed = PendingPullRequest.objects.get(branch="feat-q")
        assert owed.spec["title"] == "feat: cool thing"
        assert owed.spec["repo"] == "souliane/teatree"


class _RefusingHttpRemote(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="forge"')
        self.end_headers()


_SSH_TRANSPORTS = {
    "ssh": "echo 'git@forge.invalid: Permission denied (publickey).' >&2\nexit 255",
    "leaky": "echo 'fatal: unable to access https://forge.invalid/team/repo.git?token=hunter2' >&2\nexit 255",
    "hang": 'echo $$ > "$0.pid"\nexec sleep 30',
}


@contextmanager
def _unreadable_remote(repo: Path, transport: str) -> Iterator[None]:
    if transport in _SSH_TRANSPORTS:
        ssh = repo.parent / f"ssh-{transport}"
        ssh.write_text(f"#!/bin/sh\n{_SSH_TRANSPORTS[transport]}\n")
        ssh.chmod(0o755)
        _run_git("config", "core.sshCommand", str(ssh), cwd=repo)
        _run_git("remote", "set-url", "origin", "git@forge.invalid:team/repo.git", cwd=repo)
        yield
        return
    server = HTTPServer(("127.0.0.1", 0), _RefusingHttpRemote)
    serving = Thread(target=server.serve_forever, daemon=True)
    try:
        serving.start()
        _run_git("config", "credential.helper", "", cwd=repo)
        _run_git("remote", "set-url", "origin", f"http://127.0.0.1:{server.server_port}/team/repo.git", cwd=repo)
        yield
    finally:
        if serving.is_alive():
            server.shutdown()
        server.server_close()


class EnsurePrReadsTheRemoteBeforeOwingTestCase(TestCase):
    """What the remote says decides the obligation: present, absent, or unreadable.

    Real git throughout; only the forge's PR create is a double.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self._caplog = caplog

    def _ensure(self, repo: Path, branch: str) -> dict[str, object]:
        return cast("dict[str, object]", call_command("pr", "ensure-pr", repo=str(repo), branch=branch))

    def test_a_pushed_branch_gets_its_pr_and_the_earlier_deferral_is_discharged(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        assert self._ensure(repo, branch)["owed"] is True
        _run_git("push", "origin", branch, cwd=repo)
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://forge.example/team/repo/pull/7"}
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda _repo_path: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = self._ensure(repo, branch)

        assert result["url"] == "https://forge.example/team/repo/pull/7"
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_an_open_pr_discharges_a_deferral_the_drain_never_retried(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        self._ensure(repo, branch)
        _run_git("push", "origin", branch, cwd=repo)

        with patch.object(orphan_guard, "find_open_pr_for_branch", return_value=PrProbe.found("https://f/pr/8")):
            result = self._ensure(repo, branch)

        assert result["skipped"] == "open PR exists"
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def test_a_merged_branch_discharges_its_deferral(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        self._ensure(repo, branch)
        _run_git("checkout", "main", cwd=repo)
        _run_git("merge", "--ff-only", branch, cwd=repo)
        _run_git("push", "origin", "main", cwd=repo)

        assert "synced" in str(self._ensure(repo, branch)["skipped"])
        assert not PendingPullRequest.objects.filter(branch=branch).exists()

    def _assert_owed_as_unreadable(self, result: dict[str, object], branch: str) -> None:
        assert result["skipped"] == ensure_pr_mod.REMOTE_UNKNOWN_DEFERRAL
        assert result["owed"] is True
        assert PendingPullRequest.objects.get(branch=branch).reason == ensure_pr_mod.REMOTE_UNKNOWN_DEFERRAL

    def _ensure_behind(self, transport: str) -> None:
        repo, branch = _first_push_repo(self._tmp_path)

        with _unreadable_remote(repo, transport):
            result = self._ensure(repo, branch)

        self._assert_owed_as_unreadable(result, branch)

    def test_an_ssh_remote_refusing_the_key_owes_as_unreadable_never_as_a_first_push(self) -> None:
        self._ensure_behind("ssh")

    def test_an_https_remote_refusing_the_credential_owes_as_unreadable(self) -> None:
        self._ensure_behind("http")

    def test_a_remote_read_that_outlives_its_bound_owes_as_unreadable_and_leaves_no_transport_behind(self) -> None:
        self._monkeypatch.setattr(orphan_guard, "VERIFY_TIMEOUT_SECONDS", 1.0)
        repo, branch = _first_push_repo(self._tmp_path)

        with _unreadable_remote(repo, "hang"):
            started = time.monotonic()
            result = self._ensure(repo, branch)
            elapsed = time.monotonic() - started

        self._assert_owed_as_unreadable(result, branch)
        assert elapsed < 20
        transport_pid = int((self._tmp_path / "ssh-hang.pid").read_text())
        assert is_gone_within(transport_pid, seconds=5)

    def test_a_credential_in_the_remote_diagnostic_never_reaches_the_log(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)

        with _unreadable_remote(repo, "leaky"):
            self._ensure(repo, branch)

        assert "remote_unknown" in self._caplog.text
        assert "hunter2" not in self._caplog.text

    def test_an_unreadable_remote_keeps_the_obligation_it_cannot_settle(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        self._ensure(repo, branch)

        with _unreadable_remote(repo, "ssh"):
            result = self._ensure(repo, branch)

        self._assert_owed_as_unreadable(result, branch)

    def test_a_push_through_a_distinct_pushurl_still_leaves_its_branch_owed(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        origin = self._tmp_path / "origin.git"

        with _unreadable_remote(repo, "ssh"):
            _run_git("remote", "set-url", "--push", "origin", str(origin), cwd=repo)
            result = self._ensure(repo, branch)
            _run_git("push", "origin", branch, cwd=repo)

        _run_git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=origin)
        self._assert_owed_as_unreadable(result, branch)

    def test_a_same_named_branch_under_another_prefix_is_not_the_branch(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        _run_git("push", "origin", f"{branch}:refs/heads/team/{branch}", cwd=repo)

        assert self._ensure(repo, branch)["skipped"] == ensure_pr_mod.UNPUSHED_DEFERRAL

    def test_a_settled_run_that_owes_nothing_never_writes_the_ledger(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        _run_git("checkout", "main", cwd=repo)
        _run_git("merge", "--ff-only", branch, cwd=repo)
        _run_git("push", "origin", "main", cwd=repo)

        with CaptureQueriesContext(connection) as queries:
            result = self._ensure(repo, branch)

        assert "synced" in str(result["skipped"])
        writes = [q["sql"] for q in queries if not q["sql"].lstrip().upper().startswith("SELECT")]
        assert writes == []

    def test_a_ledger_that_cannot_be_written_never_fails_a_settled_run(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        self._ensure(repo, branch)
        _run_git("checkout", "main", cwd=repo)
        _run_git("merge", "--ff-only", branch, cwd=repo)
        _run_git("push", "origin", "main", cwd=repo)

        with patch.object(PendingPullRequest.objects, "discharge", side_effect=_LOCKED):
            result = self._ensure(repo, branch)

        assert "synced" in str(result["skipped"])
        assert "could not retire" in self._caplog.text


class DeferralSurvivesABusyControlDbTestCase(TestCase):
    """SQLite lock contention must not silently drop the obligation.

    The control DB is file-backed SQLite and the factory writes it from many
    processes at once, so a momentary ``database is locked`` is a NORMAL
    outcome of a busy box — not a broken schema. Swallowing it leaves the
    branch shipping with no PR and nothing to re-run the deferral, which is
    the exact loss this obligation exists to prevent.

    The lock is injected at the manager seam: a genuine SQLite lock is a race
    with no deterministic trigger (the unstoppable-external carve-out), and
    the behaviour under test is what ``_owe_pr`` does with it.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_transient_lock_is_retried_so_the_obligation_is_still_recorded(self) -> None:
        real_owe = PendingPullRequest.objects.owe
        attempts: list[int] = []

        def flaky_owe(**kwargs: object) -> PendingPullRequest:
            attempts.append(1)
            if len(attempts) == 1:
                raise _LOCKED
            return real_owe(**kwargs)

        with patch.object(PendingPullRequest.objects, "owe", side_effect=flaky_owe):
            result = ensure_pr_mod.defer_unpushed_pr(str(self._tmp_path / "clone"), "feat/busy")

        assert result["owed"] is True
        assert len(attempts) == 2
        assert PendingPullRequest.objects.filter(branch="feat/busy").exists()

    def test_a_lock_that_never_clears_surfaces_rather_than_claiming_a_phantom_obligation(self) -> None:
        with (
            patch.object(PendingPullRequest.objects, "owe", side_effect=_LOCKED),
            pytest.raises(OperationalError),
        ):
            ensure_pr_mod.defer_unpushed_pr(str(self._tmp_path / "clone"), "feat/wedged")

        assert not PendingPullRequest.objects.filter(branch="feat/wedged").exists()

    def test_an_unmigrated_control_db_still_degrades_to_a_warning(self) -> None:
        """The documented case: refusing the push over a missing table would wedge the box."""
        with patch.object(
            PendingPullRequest.objects,
            "owe",
            side_effect=OperationalError("no such table: teatree_pending_pull_request"),
        ):
            result = ensure_pr_mod.defer_unpushed_pr(str(self._tmp_path / "clone"), "feat/unmigrated")

        assert result["skipped"] == ensure_pr_mod.UNPUSHED_DEFERRAL


class DeferralPersistsAnAbsoluteRepoPathTestCase(TestCase):
    """The persisted ``repo_path`` is read back in a different process's cwd.

    The pre-push hook runs ``t3 teatree pr ensure-pr`` with no ``--repo``, so
    the path defaults to ``"."`` — the worktree at deferral time. The drain
    and the doctor read that value from the dispatch loop's cwd, where ``"."``
    names a different checkout entirely.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_the_production_hook_shape_records_the_worktree_not_a_dot(self) -> None:
        repo, branch = _first_push_repo(self._tmp_path)
        self._monkeypatch.chdir(repo)

        result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert result["owed"] is True
        owed = PendingPullRequest.objects.get(branch=branch)
        assert owed.repo_path == str(repo.resolve())

    def test_the_manager_refuses_to_persist_a_relative_path(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            PendingPullRequest.objects.owe(repo_path=".", branch="feat/relative", reason="x")


class ConcurrentDoubleDeferMustNotAbortThePushTestCase(TestCase):
    """Two pushes deferring the same branch at once must not break either push.

    ``owe`` is a ``get_or_create`` under ``uniq_pending_pr_repo_branch``, so the
    loser of the insert race meets an ``IntegrityError``. Escaping the pre-push
    hook, that would abort the very push the deferral exists to let through.

    The race window is simulated by making the lookup miss a row that already
    exists — the exact interleaving a second process produces.
    """

    def test_losing_the_insert_race_returns_the_existing_obligation(self) -> None:
        PendingPullRequest.objects.create(repo_path="/w/clone", branch="feat/race", reason="first defer")
        real_get = QuerySet.get
        lookups: list[int] = []

        def racy_get(queryset: QuerySet, *args: object, **kwargs: object) -> object:
            lookups.append(1)
            if len(lookups) == 1:
                raise PendingPullRequest.DoesNotExist
            return real_get(queryset, *args, **kwargs)

        with patch.object(QuerySet, "get", racy_get):
            row = PendingPullRequest.objects.owe(
                repo_path="/w/clone",
                branch="feat/race",
                reason="second defer",
            )

        assert row.reason == "second defer"
        assert PendingPullRequest.objects.filter(branch="feat/race").count() == 1


class DischargePendingIsTheDoctorFailEscapeHatchTestCase(TestCase):
    """A hard FAIL with no discharge command reddens ``t3 doctor check`` forever.

    Every other gate in the repo carries a never-lockout escape; an obligation
    the drain can never satisfy (a branch abandoned on purpose, a reaped
    worktree) needs one too, or the doctor stays red on a remediation nothing
    can run.
    """

    @staticmethod
    def _owed() -> PendingPullRequest:
        return PendingPullRequest.objects.owe(
            repo_path="/w/clone",
            branch="feat/abandoned",
            reason="branch not on remote yet",
        )

    def test_discharging_by_id_drops_the_obligation(self) -> None:
        row = self._owed()

        result = cast("dict[str, object]", call_command("pr", "discharge-pending", row.pk))

        assert result["discharged"] is True
        assert result["branch"] == "feat/abandoned"
        assert not PendingPullRequest.objects.filter(pk=row.pk).exists()

    def test_an_unknown_id_is_an_error_not_a_silent_success(self) -> None:
        result = cast("dict[str, object]", call_command("pr", "discharge-pending", 4242))

        assert result["discharged"] is False
        assert "4242" in str(result["error"])

    def test_the_doctor_fail_names_the_discharge_command(self) -> None:
        row = self._owed()
        PendingPullRequest.objects.filter(pk=row.pk).update(drain_attempts=MAX_DRAIN_ATTEMPTS)

        assert check_pending_pull_requests() is False

        out = self._capsys.readouterr().out
        assert f"pr discharge-pending {row.pk}" in out

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys


class ADisposableCheckoutOwesNothingTestCase(TestCase):
    """A cold review's scratch clone is deleted when the review ends (#4577).

    Sixteen obligations registered against such clones accumulated ~12,000 failed
    drains and sixteen permanent ``t3 doctor check`` FAILs, because the drain
    re-runs ``ensure-pr`` against a directory that no longer exists.

    Real git under ``tmp_path`` — which IS under a temp root — so the disposable
    verdict is reached by the genuine production path, not a patched predicate.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_a_first_push_from_a_temp_root_clone_owes_no_obligation(self) -> None:
        self._monkeypatch.delenv(DISPOSABLE_ROOTS_ENV, raising=False)
        repo, branch = _first_push_repo(self._tmp_path)

        result = cast("dict[str, object]", call_command("pr", "ensure-pr", repo=str(repo), branch=branch))

        assert result["skipped"] == ensure_pr_mod.DISPOSABLE_CHECKOUT_SKIP
        assert not result["owed"]
        assert not PendingPullRequest.objects.exists()

    def test_the_same_clone_outside_a_temp_root_still_owes(self) -> None:
        """The control: without it, a broken fixture would satisfy the test above too."""
        repo, branch = _first_push_repo(self._tmp_path)

        result = cast("dict[str, object]", call_command("pr", "ensure-pr", repo=str(repo), branch=branch))

        assert result["owed"] is True
        assert PendingPullRequest.objects.filter(branch=branch).exists()


class TestTheHookEntrySeesEveryOverlay:
    """A member-dir uv invocation installs only that member, so a sibling overlay's declared bot is invisible.

    Nothing then reads as unreachable, the ambient overlay's owner token is the only credential in
    sight, and the MR is opened as the owner. The entry has to install every workspace member.
    """

    _CONFIG = Path(__file__).resolve().parents[4] / ".pre-commit-config.yaml"

    def _entry(self) -> list[str]:
        repos = yaml.safe_load(self._CONFIG.read_text(encoding="utf-8"))["repos"]
        entries = [hook["entry"] for repo in repos for hook in repo["hooks"] if hook["id"] == "ensure-pr"]
        assert len(entries) == 1, "the ensure-pr hook must exist exactly once"
        argv = shlex.split(entries[0])
        return argv[1:] if argv[:1] == ["scripts/hooks/branch-push-only.sh"] else argv

    def test_the_entry_installs_every_workspace_member_before_running_the_cli(self) -> None:
        argv = self._entry()

        assert argv[:2] == ["uv", "run"]
        assert "--all-packages" in argv[: argv.index("t3")]

    def test_the_entry_still_runs_ensure_pr_through_the_teatree_prefix(self) -> None:
        assert self._entry()[-4:] == ["t3", "teatree", "pr", "ensure-pr"]
