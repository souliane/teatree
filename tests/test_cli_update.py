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
    RepoUpdate,
    UpdateStatus,
    _collect_repos,
    _ensure_self_db_migrated,
    _git_toplevel,
    _reinstall_and_resetup,
    _self_db_has_pending_migrations,
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


def _advance_remote(tmp_path: Path, bare: Path) -> str:
    """Push a new commit to the bare remote; return its short sha."""
    work = tmp_path / "advance"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(work, "config", "user.name", "Tester")
    (work / "f.txt").write_text("v2\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "second")
    _git(work, "push", "origin", "main")
    return _git(work, "rev-parse", "--short", "HEAD")


@dataclass
class _Result:
    """Recording stand-in for a discover_overlays() entry (real git, no mock)."""

    name: str
    project_path: Path | None


@dataclass
class _Proc:
    """Stand-in CompletedProcess for the host-machine `uv`/`t3` shell-outs.

    Only those externals are replaced (per the module docstring); real
    subprocess + real git drive every git-sync assertion.
    """

    returncode: int
    stdout: str
    stderr: str


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
        assert _git(clone, "rev-parse", "--short", "HEAD") == new_sha


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


class TestRepoUpdateSummary:
    def test_summary_line_shapes(self) -> None:
        updated = RepoUpdate("core", UpdateStatus.UPDATED, old_sha="aaaaaaa", new_sha="bbbbbbb")
        up = RepoUpdate("ovl", UpdateStatus.UP_TO_DATE)
        skipped = RepoUpdate("ovl2", UpdateStatus.SKIPPED, reason="dirty working tree")

        assert "aaaaaaa" in updated.summary_line
        assert "bbbbbbb" in updated.summary_line
        assert "up-to-date" in up.summary_line
        assert "skipped" in skipped.summary_line
        assert "dirty working tree" in skipped.summary_line
        assert updated.is_error is False
        assert RepoUpdate("x", UpdateStatus.FAILED, reason="boom").is_error is True


class TestUpdateCommandExitCode:
    """The typer command exits non-zero only on a hard failure, not a skip."""

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: list[RepoUpdate]) -> int:
        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("core", tmp_path)])
        monkeypatch.setattr(update_mod, "update_repo", lambda name, path: results.pop(0))
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda repos: None)
        monkeypatch.setattr(update_mod, "_ensure_self_db_migrated", lambda: False)

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
    def test_noop_when_nothing_advanced(self, capsys: pytest.CaptureFixture[str]) -> None:
        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UP_TO_DATE)])

        assert "skipping reinstall + setup" in capsys.readouterr().out

    def test_warns_when_uv_missing(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        # uv absent, t3 absent → falls back to sys.argv[0]; both echo a path.
        monkeypatch.setattr(update_mod.shutil, "which", lambda _name: None)
        monkeypatch.setattr(update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, "setup ran", ""))

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        out = capsys.readouterr().out
        assert "uv` not on PATH" in out
        assert "Re-running `t3 setup`" in out

    def test_reinstall_and_setup_run_when_advanced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = tmp_path / "editable-src"
        source.mkdir()
        calls: list[list[str]] = []

        def _which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in {"uv", "t3"} else None

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "ok", "")

        monkeypatch.setattr(update_mod.shutil, "which", _which)
        monkeypatch.setattr(setup_mod, "_current_editable_source", lambda _uv: source)
        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        out = capsys.readouterr().out
        assert "Reinstalled teatree." in out
        assert any("tool" in c and "install" in c for c in calls)
        assert any(c[-1] == "setup" for c in calls)

    def test_skips_reinstall_for_non_editable_install_still_runs_setup(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `uv` present but teatree is a non-editable install (no recorded
        # source) → reinstall is skipped, but `t3 setup` still runs.
        monkeypatch.setattr(update_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(setup_mod, "_current_editable_source", lambda _uv: None)
        monkeypatch.setattr(update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, "setup ran", ""))

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        out = capsys.readouterr().out
        assert "Reinstalling editable teatree" not in out
        assert "Re-running `t3 setup`" in out

    def test_warns_when_reinstall_and_setup_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = tmp_path / "editable-src"
        source.mkdir()

        monkeypatch.setattr(update_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(setup_mod, "_current_editable_source", lambda _uv: source)
        monkeypatch.setattr(update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(1, "", "boom"))

        _reinstall_and_resetup([RepoUpdate("core", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        out = capsys.readouterr().out
        assert "Reinstall failed" in out
        assert "`t3 setup` reported a problem" in out


class TestSelfDbMigrationOnUpdate:
    """`t3 update` applies pending teatree self-DB migrations (#871, #929).

    Before #871 updating teatree git-pulled new code (incl. new
    migrations) but never applied them. #929: the migration must be
    gated on whether migrations are actually pending — probed via
    ``manage.py migrate --check`` — NOT on whether a repo advanced
    *this run*. An interrupted prior ``t3 update`` (or an out-of-band
    ``git pull`` before ``t3 update`` runs) leaves the SHA already
    current; the next run must STILL migrate the stale self-DB, and
    must fail closed (non-zero exit) when it cannot.
    """

    def test_migrate_self_db_runs_in_runtime_interpreter_not_uv_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #126: the migrate must run in the RUNTIME process (python -m
        # teatree), resolving the runtime self-DB — NOT `uv --directory
        # <clone>`, which for a worktree-anchored editable install
        # auto-isolates onto a sibling DB the runtime never reads.
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "No migrations to apply.", "")

        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        update_mod._migrate_self_db()

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == update_mod.sys.executable, "must use the running interpreter"
        assert cmd[1:4] == ["-m", "teatree", "migrate"], f"must be `python -m teatree migrate`, got {cmd!r}"
        assert "--no-input" in cmd
        assert "--directory" not in cmd, "must NOT route through `uv --directory <clone>`"

    def test_migrate_self_db_does_not_inherit_caller_settings_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mirrors #959: a worktree-specific DJANGO_SETTINGS_MODULE in the
        # caller env must not leak into the migrate subprocess (it would
        # crash with ModuleNotFoundError). The runtime self-DB migrate
        # always runs against teatree.settings.
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "worktree_only.settings_local")
        captured: list[dict[str, str]] = []

        def _run(cmd: list[str], *, env: dict[str, str] | None = None, **_kw: object) -> _Proc:
            captured.append(dict(env or {}))
            return _Proc(0, "", "")

        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        update_mod._migrate_self_db()

        assert captured, "migrate subprocess was not invoked"
        assert captured[0].get("DJANGO_SETTINGS_MODULE") == "teatree.settings"

    def test_migrate_self_db_fails_closed_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #929: a swallowed WARN left t3 update exiting 0 with a
        # half-migrated self-DB, breaking the sanctioned merge path
        # (#870). The failure must now raise (fail-closed).
        monkeypatch.setattr(update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(1, "", "locked"))

        with pytest.raises((SystemExit, click.exceptions.Exit)):
            update_mod._migrate_self_db()

        assert "self-DB migration" in capsys.readouterr().out

    def test_self_db_probe_reports_pending_migrations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            # Django `migrate --check` exits 1 when migrations are pending.
            return _Proc(1, "", "")

        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        assert _self_db_has_pending_migrations() is True
        cmd = calls[0]
        assert cmd[0] == update_mod.sys.executable
        assert cmd[1:4] == ["-m", "teatree", "migrate"]
        assert cmd[-3:] == ["migrate", "--check", "--no-input"]

    def test_self_db_probe_reports_clean_when_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_mod, "run_allowed_to_fail", lambda *a, **k: _Proc(0, "", ""))

        assert _self_db_has_pending_migrations() is False

    def test_ensure_migrates_even_when_no_repo_advanced_this_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The #929 regression: SHA already current (no UPDATED this
        # run) but the self-DB is behind. `t3 update` MUST still migrate
        # — the migration is probe-gated, not advance-gated.
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            # First call is the `migrate --check` probe → pending (rc 1);
            # the actual `migrate` succeeds (rc 0).
            if "--check" in cmd:
                return _Proc(1, "", "")
            return _Proc(0, "Applying ...", "")

        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        failed = _ensure_self_db_migrated()

        assert failed is False
        migrate_calls = [c for c in calls if c[-2:] == ["migrate", "--no-input"]]
        assert len(migrate_calls) == 1
        assert migrate_calls[0][0] == update_mod.sys.executable

    def test_ensure_skips_migrate_when_probe_reports_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "", "")  # probe: nothing pending

        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        failed = _ensure_self_db_migrated()

        assert failed is False
        assert not [c for c in calls if c[-2:] == ["migrate", "--no-input"]]

    def test_ensure_returns_failed_when_migration_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _run(cmd: list[str], **_kw: object) -> _Proc:
            if "--check" in cmd:
                return _Proc(1, "", "")  # pending
            return _Proc(1, "", "db locked")  # migrate fails

        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        failed = _ensure_self_db_migrated()

        assert failed is True
        assert "self-DB" in capsys.readouterr().out

    def test_reinstall_flow_no_longer_migrates_self_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # #929: migration is decoupled from the reinstall flow. The
        # reinstall step must NOT run a migrate (that is now a separate
        # probe-gated step in `_run_update`).
        source = tmp_path / "editable-src"
        source.mkdir()
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> _Proc:
            calls.append(cmd)
            return _Proc(0, "ok", "")

        monkeypatch.setattr(update_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(setup_mod, "_current_editable_source", lambda _uv: source)
        monkeypatch.setattr(update_mod, "run_allowed_to_fail", _run)

        _reinstall_and_resetup([RepoUpdate("teatree", UpdateStatus.UPDATED, old_sha="a", new_sha="b")])

        assert not [c for c in calls if "migrate" in c]

    def test_run_update_fails_closed_when_self_db_migration_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: every repo up-to-date this run, but the self-DB is
        # behind and migration fails → `t3 update` exits non-zero.
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)

        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("clone", clone)])
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda _r: None)
        monkeypatch.setattr(update_mod, "_ensure_self_db_migrated", lambda: True)

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
        monkeypatch.setattr(update_mod, "_ensure_self_db_migrated", lambda: False)

        update_mod._run_update()  # no exception → exit 0

        out = capsys.readouterr().out
        assert "Summary:" in out
        assert old_sha in out
        assert new_sha in out
        assert _git(clone, "rev-parse", "--short", "HEAD") == new_sha


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
