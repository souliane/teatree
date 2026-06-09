"""Integration tests for ``t3 update``.

Real ``git init`` repos with fake (local) remotes under ``tmp_path`` — no
mocking of ``git``, ``subprocess``, or the filesystem (Test-Writing Doctrine).

The only externals stubbed are the *reinstall* and *re-run setup* side
effects: those shell out to ``uv tool install`` / ``t3 setup`` against the
host machine and are out of scope for the git-sync behaviour under test.
The stubs are recording callables, not ``Mock()`` assertions on call_args.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import click
import pytest
import typer

import teatree.cli.setup as setup_mod
import teatree.config as config_mod
from teatree.cli import update as update_mod
from teatree.cli.update import (
    ReinstallResult,
    RepoUpdate,
    UpdateStatus,
    _collect_repos,
    _git_toplevel,
    _reinstall_and_resetup,
    update_repo,
)


def _git(cwd: Path, *args: str) -> str:
    # Trusted fixed argv (literal "git" + test-controlled flags). GIT_* env
    # is stripped session-wide by conftest so the tmp repo is isolated.
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_remote(tmp_path: Path, name: str = "remote") -> Path:
    """Create a bare remote with one commit on ``main``."""
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(seed, "config", "user.name", "Tester")
    (seed / "f.txt").write_text("v1\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-m", "initial")

    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


def _clone(tmp_path: Path, bare: Path, name: str = "clone") -> Path:
    clone = tmp_path / name
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(clone, "config", "user.name", "Tester")
    return clone


def _advance_remote(tmp_path: Path, bare: Path, commits: int = 1) -> str:
    """Push *commits* new commits to the bare remote; return the head short sha."""
    work = tmp_path / "advance"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(work, "config", "user.name", "Tester")
    for n in range(commits):
        (work / "f.txt").write_text(f"v{n + 2}\n")
        _git(work, "add", "f.txt")
        _git(work, "commit", "-m", f"advance {n}")
    _git(work, "push", "origin", "main")
    return _git(work, "rev-parse", "--short", "HEAD")


@dataclass
class _Result:
    """Recording stand-in for a discover_overlays() entry (real git, no mock)."""

    name: str
    project_path: Path | None


class TestUpdateRepoCleanFastForward:
    def test_clean_on_default_branch_fast_forwards(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        old_sha = _git(clone, "rev-parse", "--short", "HEAD")
        new_sha = _advance_remote(tmp_path, bare)

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.UPDATED
        assert result.old_sha == old_sha
        assert result.new_sha == new_sha
        assert result.advanced == 1
        assert _git(clone, "rev-parse", "--short", "HEAD") == new_sha

    def test_records_count_of_commits_advanced(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare, commits=4)

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.UPDATED
        assert result.advanced == 4
        assert "+4 commits" in result.summary_line


class TestUpdateRepoUpToDate:
    def test_already_up_to_date_is_not_an_error(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.UP_TO_DATE
        assert result.is_error is False


class TestUpdateRepoUntrackedOnlyStillAdvances:
    """#924: untracked files must not block the ff-pull + reinstall.

    The autonomous review-loop writes an untracked runtime artifact
    (``.loop-review-state.json``) at the clone root.  ``git pull
    --ff-only`` and ``pip install -e`` never clobber untracked files, so
    a tree whose only 'dirt' is untracked must still fast-forward —
    otherwise the running editable ``t3`` silently rots behind
    origin/main (29 PRs stale, observed).
    """

    def test_untracked_only_does_not_block_fast_forward(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        old_sha = _git(clone, "rev-parse", "--short", "HEAD")
        new_sha = _advance_remote(tmp_path, bare)
        # The loop's own runtime artifact: untracked, never committed.
        (clone / ".loop-review-state.json").write_text('{"cursor": 1}\n')
        (clone / "scratch.tmp").write_text("ad-hoc note\n")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.UPDATED
        assert result.old_sha == old_sha
        assert result.new_sha == new_sha
        assert _git(clone, "rev-parse", "--short", "HEAD") == new_sha
        # Untracked files are preserved across the fast-forward.
        assert (clone / ".loop-review-state.json").read_text() == '{"cursor": 1}\n'
        assert (clone / "scratch.tmp").read_text() == "ad-hoc note\n"


class TestUpdateRepoSkips:
    def test_tracked_dirty_refuses_but_warns_loudly(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare)
        (clone / "f.txt").write_text("local uncommitted work\n")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.SKIPPED
        assert "tracked" in result.reason.lower()
        assert result.is_error is False
        # Never clobbered — the local edit survives.
        assert (clone / "f.txt").read_text() == "local uncommitted work\n"
        # Non-silent: a loud, prominent warning surfaces (a stale running
        # `t3` must never be invisible — #924).
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "stale" in out.lower()
        assert "clone" in out

    def test_feature_branch_checkout_skips(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature/wip")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.SKIPPED
        assert "branch" in result.reason.lower()
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "feature/wip"

    def test_non_default_branch_with_upstream_skips(self, tmp_path: Path) -> None:
        # The clone is on a tracked feature branch (real upstream), not the
        # default branch — this exercises the branch-mismatch skip *after*
        # the upstream check passes.
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature/tracked")
        _git(clone, "push", "-u", "origin", "feature/tracked")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.SKIPPED
        assert "not default" in result.reason.lower()
        assert "feature/tracked" in result.reason
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "feature/tracked"

    def test_no_upstream_skips(self, tmp_path: Path) -> None:
        seed = tmp_path / "no-remote"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
        _git(seed, "config", "user.name", "Tester")
        (seed / "f.txt").write_text("v1\n")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-m", "initial")

        result = update_repo("no-remote", seed)

        assert result.status is UpdateStatus.SKIPPED
        assert result.is_error is False


class TestPrimaryCloneOffDefaultBranchFailsLoud:
    """The primary/running clone parked off its default branch must fail loud (#2134).

    A non-default-branch (or no-upstream) primary clone must FAIL LOUD — non-zero
    exit + a prominent warning naming the current branch and the one-line fix —
    never a quiet ``SKIP`` folded into an otherwise-green summary. A stale dev
    install silently diverges from main, so the running agent keeps executing
    outdated code. Overlays keep the soft skip (they are not the editable ``t3``
    being run).
    """

    def test_primary_feature_branch_fails_loud(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature/wip")

        result = update_repo("teatree", clone, is_primary=True)

        assert result.status is UpdateStatus.FAILED
        assert result.is_error is True
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "feature/wip" in out
        assert "git switch" in out
        # Never clobbered — the clone stays on its branch.
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "feature/wip"

    def test_primary_non_default_branch_with_upstream_fails_loud(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature/tracked")
        _git(clone, "push", "-u", "origin", "feature/tracked")

        result = update_repo("teatree (running)", clone, is_primary=True)

        assert result.status is UpdateStatus.FAILED
        assert result.is_error is True
        assert "feature/tracked" in result.reason
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "feature/tracked" in out
        assert "git switch" in out

    def test_primary_no_upstream_fails_loud(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # A clone whose default branch has no upstream tracking branch (a
        # detached/local-only checkout the dev forgot to push): for the
        # primary clone this is fail-loud, not a quiet skip.
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "review-branch")

        result = update_repo("teatree", clone, is_primary=True)

        assert result.status is UpdateStatus.FAILED
        assert result.is_error is True
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "git switch" in out

    def test_overlay_off_default_branch_still_soft_skips(self, tmp_path: Path) -> None:
        # An overlay (not the running t3) parked off-branch keeps the soft
        # SKIP — only the primary clone is the fail-loud currency hazard.
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature/wip")

        result = update_repo("some-overlay", clone, is_primary=False)

        assert result.status is UpdateStatus.SKIPPED
        assert result.is_error is False

    def test_run_update_exits_nonzero_when_primary_off_default_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: _run_update collects the primary clone parked off its
        # default branch and exits non-zero (fail-loud) — not the green
        # success-shaped summary the bug produced.
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature/wip")

        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("teatree", clone)])
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda _r: None)
        monkeypatch.setattr(update_mod, "ensure_self_db_migrated", lambda: False)

        with pytest.raises((SystemExit, click.exceptions.Exit)):
            update_mod._run_update()


class TestRepoUpdateSummary:
    def test_summary_line_shapes(self) -> None:
        updated = RepoUpdate("core", UpdateStatus.UPDATED, old_sha="aaaaaaa", new_sha="bbbbbbb", advanced=3)
        up = RepoUpdate("ovl", UpdateStatus.UP_TO_DATE)
        skipped = RepoUpdate("ovl2", UpdateStatus.SKIPPED, reason="dirty working tree")

        assert "aaaaaaa" in updated.summary_line
        assert "bbbbbbb" in updated.summary_line
        assert "up-to-date" in up.summary_line
        assert "skipped" in skipped.summary_line
        assert "dirty working tree" in skipped.summary_line
        assert updated.is_error is False
        assert RepoUpdate("x", UpdateStatus.FAILED, reason="boom").is_error is True

    def test_updated_summary_reports_commit_count(self) -> None:
        one = RepoUpdate("core", UpdateStatus.UPDATED, old_sha="aaaaaaa", new_sha="bbbbbbb", advanced=1)
        many = RepoUpdate("ovl", UpdateStatus.UPDATED, old_sha="ccccccc", new_sha="ddddddd", advanced=7)

        assert "+1 commit " in one.summary_line
        assert "+1 commits" not in one.summary_line
        assert "+7 commits" in many.summary_line


class TestUpdateCommandExitCode:
    """The typer command exits non-zero only on a hard failure, not a skip."""

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: list[RepoUpdate]) -> int:
        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("core", tmp_path)])
        monkeypatch.setattr(update_mod, "update_repo", lambda name, path, *, is_primary=False: results.pop(0))
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda repos: None)
        monkeypatch.setattr(update_mod, "ensure_self_db_migrated", lambda: False)

        try:
            update_mod._run_update()
        except (SystemExit, click.exceptions.Exit) as exc:
            code = exc.code if isinstance(exc, SystemExit) else exc.exit_code
            return int(code or 0)
        return 0

    def test_skip_only_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        code = self._run(
            tmp_path,
            monkeypatch,
            [RepoUpdate("core", UpdateStatus.SKIPPED, reason="dirty working tree")],
        )
        assert code == 0

    def test_hard_failure_exits_nonzero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        code = self._run(
            tmp_path,
            monkeypatch,
            [RepoUpdate("core", UpdateStatus.FAILED, reason="git fetch failed")],
        )
        assert code != 0


class TestUpdateRepoHardFailures:
    def test_fetch_failure_is_a_hard_failure_not_a_skip(self, tmp_path: Path) -> None:
        # origin remote configured but its URL points nowhere → real `git
        # fetch` fails with a non-zero exit; that is FAILED, not SKIPPED.
        repo = tmp_path / "broken-origin"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
        _git(repo, "config", "user.name", "Tester")
        (repo / "f.txt").write_text("v1\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-m", "initial")
        _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))

        result = update_repo("broken", repo)

        assert result.status is UpdateStatus.FAILED
        assert result.is_error is True
        assert "fetch" in result.reason.lower()

    def test_no_origin_head_skips_with_reason(self, tmp_path: Path) -> None:
        # A clone whose remote exists and fetches fine, but origin/HEAD was
        # deleted — the default branch cannot be resolved → SKIPPED.
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        # A real git config a user can have: don't let fetch (re)create
        # origin/HEAD. With it removed, the default branch is unresolvable.
        _git(clone, "config", "remote.origin.followRemoteHEAD", "never")
        _git(clone, "remote", "set-head", "origin", "--delete")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.SKIPPED
        assert "origin/head" in result.reason.lower()
        assert result.is_error is False

    def test_non_fast_forward_pull_is_a_hard_failure(self, tmp_path: Path) -> None:
        # Local default branch has a committed divergence from the remote, so
        # `git pull --ff-only` cannot fast-forward → FAILED (never rebased).
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare)
        (clone / "f.txt").write_text("divergent local commit\n")
        _git(clone, "add", "f.txt")
        _git(clone, "commit", "-m", "local divergence")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.FAILED
        assert result.is_error is True
        assert "ff-only" in result.reason.lower()


class TestGitToplevel:
    def test_returns_none_for_non_directory(self, tmp_path: Path) -> None:
        assert _git_toplevel(tmp_path / "missing") is None

    def test_returns_none_for_non_git_directory(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _git_toplevel(plain) is None

    def test_resolves_subdir_to_work_tree_root(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        sub = clone / "nested" / "deep"
        sub.mkdir(parents=True)

        assert _git_toplevel(sub) == clone.resolve()


class TestCollectRepos:
    def test_collects_core_and_overlay_dedups(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        core_bare = _make_remote(tmp_path, "core")
        core = _clone(tmp_path, core_bare, "core-clone")
        ovl_bare = _make_remote(tmp_path, "ovl")
        ovl = _clone(tmp_path, ovl_bare, "ovl-clone")

        monkeypatch.setattr(setup_mod, "_find_main_clone", lambda: core)
        monkeypatch.setattr(
            config_mod,
            "discover_overlays",
            lambda: [
                _Result("ovl", ovl),
                _Result("dup-core", core),  # same repo as core → deduped
                _Result("no-path", None),  # entry without a project path
            ],
        )

        repos = _collect_repos()

        assert ("teatree", core.resolve()) in repos
        assert ("ovl", ovl.resolve()) in repos
        names = [n for n, _ in repos]
        assert "dup-core" not in names
        assert "no-path" not in names

    def test_handles_missing_core(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ovl_bare = _make_remote(tmp_path, "ovl")
        ovl = _clone(tmp_path, ovl_bare, "ovl-clone")

        monkeypatch.setattr(setup_mod, "_find_main_clone", lambda: None)
        monkeypatch.setattr(update_mod, "_running_clone", lambda: None)
        monkeypatch.setattr(config_mod, "discover_overlays", lambda: [_Result("ovl", ovl)])

        repos = _collect_repos()

        assert repos == [("ovl", ovl.resolve())]

    def test_includes_the_clone_the_interpreter_runs_from(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worktree-anchored entrypoint is audited for currency (#1507).

        ``_find_main_clone`` reports the *configured* main clone (cwd/T3_REPO),
        but the editable ``.pth`` can be anchored to a worktree the interpreter
        actually imports from. Unless the running clone is collected, a stale
        worktree-anchored install sails past the #948 clone-currency gate.
        """
        core_bare = _make_remote(tmp_path, "core")
        core = _clone(tmp_path, core_bare, "core-clone")
        running_bare = _make_remote(tmp_path, "running")
        running = _clone(tmp_path, running_bare, "running-clone").resolve()

        monkeypatch.setattr(setup_mod, "_find_main_clone", lambda: core)
        monkeypatch.setattr(config_mod, "discover_overlays", list)
        monkeypatch.setattr(update_mod, "_running_clone", lambda: running)

        repos = _collect_repos()

        assert ("teatree (running)", running) in repos


class TestReinstallAndResetup:
    """``_reinstall_and_resetup`` orchestrates the shared reinstall seam.

    The actual ``uv tool install`` + ``t3 setup`` mechanics live in
    :func:`teatree.self_update.reinstall_running_editable` (tested in
    ``test_self_update.py``); these assert only the CLI-side orchestration:
    skip when nothing advanced, and surface the seam's outcome.
    """

    def test_noop_when_nothing_advanced(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        called: list[bool] = []
        monkeypatch.setattr(update_mod, "reinstall_running_editable", lambda: called.append(True))

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UP_TO_DATE)])

        assert "skipping reinstall + setup" in capsys.readouterr().out
        assert called == [], "the reinstall seam must not run when nothing advanced"

    def test_reports_success_when_advanced(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(update_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(
            update_mod,
            "reinstall_running_editable",
            lambda: ReinstallResult(ok=True, reinstalled=True),
        )

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        out = capsys.readouterr().out
        assert "Reinstalled teatree." in out
        assert "`t3 setup` complete." in out

    def test_warns_when_uv_missing(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(update_mod.shutil, "which", lambda _name: None)
        monkeypatch.setattr(
            update_mod,
            "reinstall_running_editable",
            lambda: ReinstallResult(ok=True, reinstalled=False),
        )

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        assert "uv` not on PATH" in capsys.readouterr().out

    def test_warns_when_seam_reports_problem(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(update_mod.shutil, "which", lambda _name: "/usr/bin/uv")
        monkeypatch.setattr(
            update_mod,
            "reinstall_running_editable",
            lambda: ReinstallResult(ok=False, reinstalled=False, error="setup: boom"),
        )

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        assert "reinstall/setup reported a problem: setup: boom" in capsys.readouterr().out


class TestSelfDbMigrationOnUpdate:
    """End-to-end: a stale self-DB whose migration fails must fail the run.

    The probe/migrate mechanics live in ``test_self_update.py``; this asserts
    only that ``_run_update`` fails closed (non-zero exit) when the self-DB is
    left unmigrated (#929 / #870).
    """

    def test_run_update_fails_closed_when_self_db_migration_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: every repo up-to-date this run, but the self-DB is
        # behind and migration fails → `t3 update` exits non-zero.
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)

        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("clone", clone)])
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda _r: None)
        monkeypatch.setattr(update_mod, "ensure_self_db_migrated", lambda: True)

        with pytest.raises((SystemExit, click.exceptions.Exit)):
            update_mod._run_update()


class TestRunCallback:
    def test_callback_returns_early_for_subcommand(self) -> None:
        ctx = typer.Context(click.Command("update"))
        ctx.invoked_subcommand = "slack-bot"
        # Should not raise / not run the flow.
        update_mod.run(ctx)

    def test_callback_runs_flow_when_no_subcommand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran: list[bool] = []
        monkeypatch.setattr(update_mod, "_run_update", lambda: ran.append(True))
        ctx = typer.Context(click.Command("update"))
        ctx.invoked_subcommand = None

        update_mod.run(ctx)

        assert ran == [True]


class TestRunUpdateEndToEnd:
    def test_no_repos_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(update_mod, "_collect_repos", list)

        with pytest.raises((SystemExit, click.exceptions.Exit)):
            update_mod._run_update()

        assert "No teatree core or overlay repos found" in capsys.readouterr().out

    def test_real_repo_summary_and_zero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        old_sha = _git(clone, "rev-parse", "--short", "HEAD")
        new_sha = _advance_remote(tmp_path, bare)

        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("clone", clone)])
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda _r: None)
        monkeypatch.setattr(update_mod, "ensure_self_db_migrated", lambda: False)

        update_mod._run_update()  # no exception → exit 0

        out = capsys.readouterr().out
        assert "Summary:" in out
        assert old_sha in out
        assert new_sha in out
        assert "+1 commit " in out
        assert _git(clone, "rev-parse", "--short", "HEAD") == new_sha

    def test_summary_reports_multi_commit_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare, commits=3)

        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("clone", clone)])
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda _r: None)
        monkeypatch.setattr(update_mod, "ensure_self_db_migrated", lambda: False)

        update_mod._run_update()

        assert "+3 commits" in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
