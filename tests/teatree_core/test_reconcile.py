"""Tests for teatree.core.worktree.reconcile — state drift detector."""

import shutil
import subprocess
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.worktree.branch_classification as bc
from teatree.core.models import Ticket, Worktree
from teatree.core.models.merge_clear import MergeAudit, MergeClear
from teatree.core.overlay import OverlayBase
from teatree.core.worktree.reconcile import (
    DoneButUnmerged,
    Drift,
    DuplicateScope,
    EnvCacheDrift,
    MissingEnvCache,
    MissingWorktreeDir,
    UnpushedWork,
    UnresolvableOverlay,
    _collect_stale_worktree_dirs,
    _done_but_unmerged_for_ticket,
    _duplicate_scope_for_ticket,
    _unpushed_work_for_worktree,
    reconcile_all,
    reconcile_ticket,
    reconcile_work_state_all,
)
from teatree.core.worktree.worktree_env import write_env_cache
from teatree.utils import git
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git
from tests.teatree_core.conftest import CommandOverlay

_COMMAND = {"test": CommandOverlay()}


def _make_ghost(tmp: str, *, dir_name: str = "ticket-ghost") -> tuple[Ticket, Worktree, Path]:
    """A provisioned worktree whose overlay name is not registered anywhere.

    ``get_overlay_for_worktree`` raises ``ImproperlyConfigured`` for it, the
    same way a row for an overlay uninstalled in this environment does.
    """
    ticket_dir = Path(tmp) / dir_name
    ticket_dir.mkdir()
    wt_path = ticket_dir / "backend"
    wt_path.mkdir()
    ticket = Ticket.objects.create(overlay="t3-ghost", issue_url="https://ex.com/ghost")
    wt = Worktree.objects.create(
        overlay="t3-ghost",
        ticket=ticket,
        repo_path="backend",
        branch="ghost",
        db_name="",
        extra={"worktree_path": str(wt_path)},
        state=Worktree.State.PROVISIONED,
    )
    return ticket, wt, wt_path


class _PgUserOverlay(CommandOverlay):
    """Overlay that connects to postgres as a non-default superuser role."""

    def get_env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"POSTGRES_USER": "db_superuser", "POSTGRES_HOST": "localhost"}


def _make(tmp: str, *, db_name: str = "wt_99") -> tuple[Ticket, Worktree, Path]:
    ticket_dir = Path(tmp) / "ticket-99"
    ticket_dir.mkdir()
    wt_path = ticket_dir / "backend"
    wt_path.mkdir()
    ticket = Ticket.objects.create(overlay="test", issue_url="https://ex.com/99", variant="acme")
    wt = Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        db_name=db_name,
        extra={"worktree_path": str(wt_path)},
        state=Worktree.State.PROVISIONED,
    )
    return ticket, wt, wt_path


class TestDriftDataclass(TestCase):
    def test_has_drift_false_for_empty(self) -> None:
        drift = Drift(ticket_pk=1)
        assert not drift.has_drift
        assert drift.format() == "(no drift)"

    def test_has_drift_true_with_any_finding(self) -> None:
        drift = Drift(ticket_pk=1, missing_env_caches=[MissingEnvCache(worktree_pk=5, cache_path=Path("/x"))])
        assert drift.has_drift
        assert "missing-env-cache" in drift.format()

    def test_work_state_findings_render_for_doctor(self) -> None:
        # workspace doctor surfaces the SELFCATCH-1 findings for free via format().
        drift = Drift(
            ticket_pk=1,
            unpushed_work=[UnpushedWork(worktree_pk=5, branch="feature", shas=["abc feat: x"])],
            done_but_unmerged=[DoneButUnmerged(ticket_pk=1, branch="feature", reason="no merge audit")],
            duplicate_scopes=[DuplicateScope(issue_number="42", paths=[Path("/w/42-a"), Path("/w/42-b")])],
        )
        rendered = drift.format()
        assert drift.has_drift
        assert "unpushed-work: wt#5 feature" in rendered
        assert "done-but-unmerged: ticket#1 feature" in rendered
        assert "duplicate-scope: issue 42" in rendered

    def test_unpushed_probe_error_renders_reason(self) -> None:
        drift = Drift(
            ticket_pk=1, unpushed_work=[UnpushedWork(worktree_pk=5, branch="feature", probe_error="git boom")]
        )
        assert "probe inconclusive: git boom" in drift.format()


class TestReconcileTicket(TestCase):
    def test_detects_missing_env_cache(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.worktree.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", return_value=True),
        ):
            ticket, _, _ = _make(tmp)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_env_caches) == 1
        assert drift.missing_env_caches[0].worktree_pk
        assert drift.has_drift

    def test_detects_env_cache_drift(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.worktree.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, _ = _make(tmp)
            spec = write_env_cache(wt)
            assert spec is not None
            # Tamper with the cache on disk.
            spec.path.chmod(0o644)
            spec.path.write_text("tampered\n", encoding="utf-8")
            drift = reconcile_ticket(ticket)
        assert len(drift.env_cache_drifts) == 1
        assert isinstance(drift.env_cache_drifts[0], EnvCacheDrift)

    def test_detects_missing_worktree_dir(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.worktree.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, wt_path = _make(tmp)
            write_env_cache(wt)
            shutil.rmtree(wt_path)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_worktree_dirs) == 1
        assert isinstance(drift.missing_worktree_dirs[0], MissingWorktreeDir)

    def test_detects_orphan_containers_when_worktree_torn_down(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch(
                "teatree.core.worktree.reconcile._find_docker_containers",
                return_value=["backend-wt99-web-1"],
            ),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, _ = _make(tmp)
            wt.state = Worktree.State.CREATED  # post-teardown
            wt.save()
            drift = reconcile_ticket(ticket)
        assert len(drift.orphan_containers) == 1
        assert drift.orphan_containers[0].name == "backend-wt99-web-1"

    def test_clean_state_reports_no_drift(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.worktree.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", return_value=True),
        ):
            ticket, wt, _ = _make(tmp)
            write_env_cache(wt)
            drift = reconcile_ticket(ticket)
        assert not drift.has_drift


class TestReconcileMissingDbUsesWorktreePgUser(TestCase):
    """An existing DB reachable only as the overlay's role must not read as missing.

    The bug: ``db_exists`` connected with the bare process-env default
    ``POSTGRES_USER`` (``postgres`` — a role that need not exist), so a
    DB owned by a non-default superuser role reported missing for many
    tickets. ``doctor --fix`` then nudges a re-provision that drops the
    good DB. The reconciler must connect with the worktree's resolved role.
    """

    _OVERLAYS: ClassVar[dict[str, OverlayBase]] = {"test": _PgUserOverlay()}

    def _existing_only_as_superuser(self, db_name: str, *, user: str = "", **_: object) -> bool:
        # Mirrors the host: the DB exists, but only the overlay's role can see it.
        # The default ``postgres`` connection fails and yields no rows -> False.
        return user == "db_superuser" and db_name == "wt_99"

    def test_existing_db_not_reported_missing_when_owned_by_overlay_role(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=self._OVERLAYS),
            patch("teatree.core.worktree.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", side_effect=self._existing_only_as_superuser),
        ):
            ticket, wt, _ = _make(tmp)
            write_env_cache(wt)
            drift = reconcile_ticket(ticket)
        assert drift.missing_dbs == []

    def test_genuinely_absent_db_still_reported_missing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=self._OVERLAYS),
            patch("teatree.core.worktree.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", return_value=False),
        ):
            ticket, wt, _ = _make(tmp)
            write_env_cache(wt)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_dbs) == 1
        assert drift.missing_dbs[0].db_name == "wt_99"


class TestReconcileUnresolvableOverlay(TestCase):
    """A row whose overlay is not installed here must not abort the sweep (#2472)."""

    _PATCHES: ClassVar[tuple] = ()

    def _patches(self):
        return (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND),
            patch("teatree.core.worktree.reconcile._find_docker_containers", return_value=[]),
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value=set()),
            patch("teatree.core.worktree.reconcile.db_exists", return_value=True),
        )

    def test_records_unresolvable_overlay_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for ctx in self._patches():
                self.enterContext(ctx)
            ticket, wt, _ = _make_ghost(tmp)
            drift = reconcile_ticket(ticket)
        assert len(drift.unresolvable_overlays) == 1
        assert isinstance(drift.unresolvable_overlays[0], UnresolvableOverlay)
        assert drift.unresolvable_overlays[0].worktree_pk == wt.pk
        assert drift.unresolvable_overlays[0].overlay == "t3-ghost"
        assert drift.has_drift
        assert "unresolvable-overlay" in drift.format()

    def test_still_detects_missing_dir_for_unresolvable_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for ctx in self._patches():
                self.enterContext(ctx)
            ticket, _, wt_path = _make_ghost(tmp)
            shutil.rmtree(wt_path)
            drift = reconcile_ticket(ticket)
        assert len(drift.missing_worktree_dirs) == 1
        assert len(drift.unresolvable_overlays) == 1

    def test_reconcile_all_isolates_the_unresolvable_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for ctx in self._patches():
                self.enterContext(ctx)
            ghost_ticket, _, _ = _make_ghost(tmp)
            ok_ticket, ok_wt, ok_path = _make(tmp)
            write_env_cache(ok_wt)
            shutil.rmtree(ok_path)
            drifts = reconcile_all()
        # The ghost ticket is surfaced (unresolvable) AND the installed-overlay
        # ticket is still reconciled — the sweep no longer aborts on the ghost.
        assert ghost_ticket.pk in drifts
        assert ok_ticket.pk in drifts
        assert len(drifts[ok_ticket.pk].missing_worktree_dirs) == 1


class TestStaleWorktreeDirAttributionIsSegmentAnchored(TestCase):
    """Stale-dir attribution anchors the ticket-number on path segments (#WT-PR-D finding 17).

    ``/9`` must not match ``/90``: the pre-fix raw substring (``f"/{n}" in path``)
    mis-attributed an unrelated ticket-90 dir to ticket 9.
    """

    def _ticket9_with_wt(self) -> tuple[Ticket, Worktree]:
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/9")
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="repo",
            branch="9-fix",
            extra={"worktree_path": "/ws/9-fix/repo"},
        )
        return ticket, wt

    def test_ticket90_dir_not_attributed_to_ticket9(self) -> None:
        ticket, wt = self._ticket9_with_wt()
        drift = Drift(ticket_pk=ticket.pk)
        foreign = "/ws/90-other/repo"  # belongs to ticket 90, not 9
        with (
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value={foreign}),
            patch("teatree.core.worktree.reconcile.resolve_clone_path", return_value=Path("/ws/repo")),
        ):
            _collect_stale_worktree_dirs(drift, [wt], ticket, Path("/ws"))

        assert drift.stale_worktree_dirs == []

    def test_genuine_ticket9_dir_is_attributed(self) -> None:
        ticket, wt = self._ticket9_with_wt()
        drift = Drift(ticket_pk=ticket.pk)
        genuine = "/ws/9-elsewhere/repo"  # a stale dir genuinely for ticket 9
        with (
            patch("teatree.core.worktree.reconcile._find_worktree_paths_on_disk", return_value={genuine}),
            patch("teatree.core.worktree.reconcile.resolve_clone_path", return_value=Path("/ws/repo")),
        ):
            _collect_stale_worktree_dirs(drift, [wt], ticket, Path("/ws"))

        assert len(drift.stale_worktree_dirs) == 1
        assert str(drift.stale_worktree_dirs[0].path) == genuine


# ── SELFCATCH-1: work-tracking-truth findings ────────────────────────


def _init_repo(tmp: Path) -> Path:
    """A bare origin + a work clone with one base commit on main pushed to origin."""
    remote = tmp / "remote.git"
    subprocess.run(
        [_GIT, "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    work = tmp / "work"
    work.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=work)
    _run_git("config", "user.email", "t@t", cwd=work)
    _run_git("config", "user.name", "t", cwd=work)
    _run_git("remote", "add", "origin", str(remote), cwd=work)
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "initial", cwd=work)
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)
    return work


def _branch_with_unpushed_commit(work: Path, branch: str, fname: str, subject: str) -> None:
    """Create ``branch`` off main with one committed-but-unpushed commit, then return to main."""
    _run_git("checkout", "-q", "-b", branch, "main", cwd=work)
    (work / fname).write_text("x\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", subject, cwd=work)
    _run_git("checkout", "-q", "main", cwd=work)


def _no_forge() -> AbstractContextManager[object]:
    """Stub the forge merge-state probe absent so only deterministic git content decides."""
    return patch.object(bc, "probe_host_cli", return_value="")


class TestUnpushedWorkFinding(TestCase):
    """A live worktree carrying commits absent from every remote is surfaced (PR-01/PR-25 class)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _wt(self, wt_path: Path, branch: str) -> Worktree:
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/7")
        return Worktree.objects.create(
            ticket=ticket, repo_path="repo", branch=branch, extra={"worktree_path": str(wt_path)}
        )

    def test_committed_unpushed_commit_is_flagged(self) -> None:
        work = _init_repo(self.tmp)
        _branch_with_unpushed_commit(work, "feature", "new.txt", "feat: unpushed work")
        _run_git("checkout", "-q", "feature", cwd=work)
        finding = _unpushed_work_for_worktree(self._wt(work, "feature"))
        assert isinstance(finding, UnpushedWork)
        assert finding.shas
        assert not finding.probe_error
        assert "feat: unpushed work" in finding.shas[0]

    def test_fully_pushed_worktree_is_not_flagged(self) -> None:
        work = _init_repo(self.tmp)  # HEAD == origin/main, nothing local
        assert _unpushed_work_for_worktree(self._wt(work, "main")) is None

    def test_non_git_dir_is_not_flagged(self) -> None:
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        assert _unpushed_work_for_worktree(self._wt(plain, "main")) is None

    def test_inconclusive_probe_is_a_finding_not_silent_pass(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=empty)  # a git worktree with an unresolvable HEAD
        finding = _unpushed_work_for_worktree(self._wt(empty, "main"))
        assert isinstance(finding, UnpushedWork)
        assert finding.probe_error
        assert not finding.shas


class TestDoneButUnmergedFinding(TestCase):
    """A ticket marked done whose branch never merged is surfaced (believe-done-what-isn't)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _done_ticket(self, branch: str, work: Path) -> tuple[Ticket, Worktree]:
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/8", state=Ticket.State.MERGED)
        wt = Worktree.objects.create(ticket=ticket, repo_path="repo", branch=branch, extra={"clone_path": str(work)})
        return ticket, wt

    def test_done_ticket_unmerged_branch_flagged(self) -> None:
        work = _init_repo(self.tmp)
        _branch_with_unpushed_commit(work, "feature", "ahead.txt", "feat: never merged")
        ticket, wt = self._done_ticket("feature", work)
        with _no_forge():
            finding = _done_but_unmerged_for_ticket(ticket, [wt], self.tmp)
        assert isinstance(finding, DoneButUnmerged)
        assert finding.branch == "feature"
        assert "unmerged commit" in finding.reason

    def test_done_ticket_with_merge_audit_not_flagged(self) -> None:
        work = _init_repo(self.tmp)
        _branch_with_unpushed_commit(work, "feature", "ahead.txt", "feat: merged")
        ticket, wt = self._done_ticket("feature", work)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=8,
            slug="repo",
            reviewed_sha="a" * 40,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        MergeAudit.objects.create(clear=clear, merged_sha="b" * 40, required_checks_status="green")
        with _no_forge():
            assert _done_but_unmerged_for_ticket(ticket, [wt], self.tmp) is None

    def test_done_ticket_upstream_branch_not_flagged(self) -> None:
        work = _init_repo(self.tmp)  # branch "main" is fully upstream (redundant)
        ticket, wt = self._done_ticket("main", work)
        with _no_forge():
            assert _done_but_unmerged_for_ticket(ticket, [wt], self.tmp) is None

    def test_inconclusive_branch_probe_is_a_finding(self) -> None:
        work = _init_repo(self.tmp)  # branch does not exist → git cherry fails → inconclusive
        ticket, wt = self._done_ticket("ghost-branch", work)
        with _no_forge():
            finding = _done_but_unmerged_for_ticket(ticket, [wt], self.tmp)
        assert isinstance(finding, DoneButUnmerged)
        assert "inconclusive" in finding.reason

    def test_non_done_ticket_not_flagged(self) -> None:
        work = _init_repo(self.tmp)
        _branch_with_unpushed_commit(work, "feature", "ahead.txt", "feat: in progress")
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/8", state=Ticket.State.STARTED)
        wt = Worktree.objects.create(ticket=ticket, repo_path="repo", branch="feature", extra={"clone_path": str(work)})
        with _no_forge():
            assert _done_but_unmerged_for_ticket(ticket, [wt], self.tmp) is None


class TestDuplicateScopeFinding(TestCase):
    """More than one worktree dir for the same issue scope is surfaced (blind-redo)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wt_root = self.tmp / "wtroot"
        self.wt_root.mkdir()

    def _ticket(self, branch: str) -> Ticket:
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/42")
        ticket.extra = {"branch": branch}
        ticket.save()
        return ticket

    def test_two_worktree_dirs_for_one_issue_flagged(self) -> None:
        (self.wt_root / "42-first").mkdir()
        (self.wt_root / "42-second").mkdir()
        ticket = self._ticket("42-first")
        finding = _duplicate_scope_for_ticket(ticket, [], self.wt_root)
        assert isinstance(finding, DuplicateScope)
        assert finding.issue_number == "42"
        assert any(p.name == "42-second" for p in finding.paths)

    def test_single_scope_not_flagged(self) -> None:
        (self.wt_root / "42-first").mkdir()
        ticket = self._ticket("42-first")
        assert _duplicate_scope_for_ticket(ticket, [], self.wt_root) is None


class TestReconcileWorkStateAll(TestCase):
    """The loop entry point surfaces work-state drift and stays read-only."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_surfaces_unpushed_and_stays_read_only(self) -> None:
        work = _init_repo(self.tmp)
        _branch_with_unpushed_commit(work, "feature", "new.txt", "feat: unpushed")
        _run_git("checkout", "-q", "feature", cwd=work)
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/7")
        Worktree.objects.create(ticket=ticket, repo_path="repo", branch="feature", extra={"worktree_path": str(work)})
        with _no_forge():
            drifts = reconcile_work_state_all()
        assert ticket.pk in drifts
        assert drifts[ticket.pk].unpushed_work
        # Read-only: the surfaced work is NOT auto-pushed — it stays unpushed.
        assert git.commits_absent_from_all_remotes(str(work), "HEAD")

    def test_clean_worktree_yields_no_findings(self) -> None:
        work = _init_repo(self.tmp)
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/7")
        Worktree.objects.create(ticket=ticket, repo_path="repo", branch="main", extra={"worktree_path": str(work)})
        with _no_forge():
            assert reconcile_work_state_all() == {}
