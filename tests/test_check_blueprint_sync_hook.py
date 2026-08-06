"""Tests for the BLUEPRINT-sync commit-msg hook (souliane/teatree#8).

The hook fails when ``src/`` changes without a corresponding BLUEPRINT update,
unless the commit type is exempt (test/docs/style/chore/ci/fix/refactor). The
"BLUEPRINT" is the top-level ``BLUEPRINT.md`` plus its split appendix files
under ``docs/blueprint/`` — updating an appendix satisfies the requirement just
as the monolith does (teatree#2237: the appendices ARE the BLUEPRINT).

That type exemption is withdrawn for a commit staging the shipped
``config/defaults.toml`` or an enum vocabulary — the values the docs quote
literally. #3895's ``chore(config)`` defaults flip invalidated the mode/wip/
agent_runtime prose and passed the gate by prefix alone.

The exemption depends on the hook reading the *commit message*. The hook must
therefore source the commit type robustly — from the commit-message file git
hands it at the ``commit-msg`` stage, and never from a staged source filename it
might be handed at another invocation (pre-commit stage / ``prek run
--all-files``). The latter coupling is the bug behind task #35: a positional
argument that is a ``src/`` path was mis-read as the commit message, so the
``fix:``/``refactor:`` exemption could never match and a ``fix(db)`` commit was
gated.

``src/`` and ``BLUEPRINT.md`` name teatree's OWN tree, which is the git repo root
only in a plain clone. :class:`TestVendoredLayout` and :class:`TestStandaloneLayout`
run the shipped script against real git checkouts of both layouts, because that
mapping is the part no in-process test of :func:`_is_blueprint` can reach.
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.hooks import check_blueprint_sync as hook


@dataclass
class _RunOptions:
    """``TestMain._run``'s optional invocation flags."""

    argv_is_src: bool = False
    is_merge_commit: bool = False
    is_revert_commit: bool = False


class TestIsBlueprint:
    @pytest.mark.parametrize(
        "path",
        [
            "BLUEPRINT.md",
            "docs/blueprint/configuration.md",
            "docs/blueprint/loop-topology.md",
            "docs/blueprint/factory-architecture.md",
        ],
    )
    def test_blueprint_paths_count(self, path: str) -> None:
        assert hook._is_blueprint(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "docs/dependency-graph.md",
            "docs/blueprint/notes.txt",
            "src/teatree/config/agent_spawn.py",
            "README.md",
            "docs/blueprintish.md",
        ],
    )
    def test_non_blueprint_paths_do_not_count(self, path: str) -> None:
        assert hook._is_blueprint(path) is False


class TestLooksLikeCommitMsgFile:
    """A commit-message file is distinguished from a staged source filename.

    The commit-type source must read git's commit-message file, never a staged
    source filename a non-commit-msg invocation might hand the hook as argv[1].
    """

    @pytest.mark.parametrize(
        "path",
        [
            ".git/COMMIT_EDITMSG",
            "/repo/.git/COMMIT_EDITMSG",
            "/repo/.git/worktrees/wt/COMMIT_EDITMSG",
            ".git/MERGE_MSG",
        ],
    )
    def test_commit_msg_files_are_recognized(self, path: str) -> None:
        assert hook._looks_like_commit_msg_file(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/config/agent_spawn.py",
            "scripts/hooks/check_blueprint_sync.py",
            "BLUEPRINT.md",
            "docs/blueprint/configuration.md",
            "tests/test_check_blueprint_sync_hook.py",
        ],
    )
    def test_source_filenames_are_not_commit_msg_files(self, path: str) -> None:
        assert hook._looks_like_commit_msg_file(path) is False


class TestCommitMessage:
    def test_reads_message_from_commit_msg_argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("fix(db): a thing\n", encoding="utf-8")
        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", str(msg_file)])
        assert hook._commit_message() == "fix(db): a thing"

    def test_ignores_staged_source_filename_argv_and_falls_back_to_git_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The regression: a non-commit-msg argv (a staged src path, as handed at
        # the pre-commit stage / `prek run --all-files`) must NOT be read as the
        # commit message. The hook falls back to git's canonical COMMIT_EDITMSG.
        src_file = tmp_path / "src" / "teatree" / "foo.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("import pathlib\n", encoding="utf-8")

        canonical = tmp_path / "COMMIT_EDITMSG"
        canonical.write_text("fix(db): a thing\n", encoding="utf-8")
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(canonical))

        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", "src/teatree/foo.py"])
        # Must read the real commit message from git's canonical path, not the
        # first line of the staged source file.
        assert hook._commit_message() == "fix(db): a thing"

    def test_no_argv_falls_back_to_git_canonical_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        canonical = tmp_path / "COMMIT_EDITMSG"
        canonical.write_text("refactor(core): extract helper\n", encoding="utf-8")
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(canonical))
        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py"])
        assert hook._commit_message() == "refactor(core): extract helper"

    def test_missing_message_everywhere_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(tmp_path / "nope"))
        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py"])
        assert hook._commit_message() == ""


class TestMain:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        message: str,
        staged: list[str],
        options: _RunOptions | None = None,
    ) -> int:
        options = options or _RunOptions()
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text(message + "\n", encoding="utf-8")
        # Git's canonical commit-msg path always carries the real message.
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(msg_file))
        if options.argv_is_src:
            # Simulate the buggy invocation: a staged src path as argv[1].
            monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", "src/teatree/x.py"])
        else:
            monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", str(msg_file)])
        monkeypatch.setattr(hook, "_staged_files", lambda: staged)
        # The staged literals below are repo-root-relative, i.e. the plain-clone
        # layout; the vendored one is covered against a real checkout instead.
        monkeypatch.setattr(hook, "_vendoring_prefix", lambda: "")
        monkeypatch.setattr(hook, "_is_merge_commit", lambda: options.is_merge_commit)
        monkeypatch.setattr(hook, "_is_revert_commit", lambda: options.is_revert_commit)
        return hook.main()

    def test_src_without_blueprint_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(agent): something",
            staged=["src/teatree/config/agent_spawn.py"],
        )
        assert rc == 1

    def test_src_with_top_level_blueprint_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(agent): something",
            staged=["src/teatree/config/agent_spawn.py", "BLUEPRINT.md"],
        )
        assert rc == 0

    def test_src_with_appendix_blueprint_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Documenting in a docs/blueprint/ appendix satisfies the sync gate, so a
        # feat commit need not touch BLUEPRINT.md.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(agent): single-toggle model pin override",
            staged=["src/teatree/config/agent_spawn.py", "docs/blueprint/configuration.md"],
        )
        assert rc == 0

    def test_exempt_commit_type_passes_without_blueprint(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="fix(agent): a bug",
            staged=["src/teatree/config/agent_spawn.py"],
        )
        assert rc == 0

    def test_refactor_commit_type_passes_without_blueprint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="refactor(core): extract helper",
            staged=["src/teatree/config/agent_spawn.py"],
        )
        assert rc == 0

    def test_no_src_change_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(docs): docs only",
            staged=["docs/blueprint/configuration.md"],
        )
        assert rc == 0

    def test_fix_commit_exempt_even_when_argv_is_staged_src_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Task #35 regression: when the hook is handed a staged src path as
        # argv[1] (pre-commit stage / `prek run --all-files`) instead of the
        # commit-message file, the fix: exemption must STILL fire by sourcing
        # the commit type from git's canonical COMMIT_EDITMSG.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="fix(db): reconcile renumbered migration records",
            staged=["src/teatree/db.py"],
            options=_RunOptions(argv_is_src=True),
        )
        assert rc == 0

    def test_feat_commit_still_gated_when_argv_is_staged_src_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The fix must not over-correct: a feat commit needing a BLUEPRINT
        # update is still gated even when argv[1] is a staged src path, because
        # the commit type is sourced from git's canonical COMMIT_EDITMSG.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(db): a new capability",
            staged=["src/teatree/db.py"],
            options=_RunOptions(argv_is_src=True),
        )
        assert rc == 1

    def test_merge_commit_is_exempt_even_with_src_and_no_blueprint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A merge commit's default message ("Merge branch 'origin/main' into
        # <branch>") matches no exempt prefix, and its staged tree carries
        # every upstream commit's src/ changes without necessarily carrying a
        # matching BLUEPRINT update in the SAME commit — merging any non-
        # BLUEPRINT upstream source commit would otherwise always false-block.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="Merge branch 'origin/main' into some-branch",
            staged=["src/teatree/config/agent_spawn.py"],
            options=_RunOptions(is_merge_commit=True),
        )
        assert rc == 0

    def test_non_merge_commit_with_unexempt_message_still_gated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The merge exemption must not over-correct: an ordinary (non-merge)
        # commit with a message that happens to start with "Merge" is still
        # gated when it isn't actually mid-merge.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="Merge in the new config helper",
            staged=["src/teatree/config/agent_spawn.py"],
            options=_RunOptions(is_merge_commit=False),
        )
        assert rc == 1

    def test_revert_commit_is_exempt_even_with_src_and_no_blueprint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # git revert's default message ("Revert \"...\"") matches no exempt
        # prefix, and the staged tree is the inverse of a single original
        # commit's diff — if that commit never touched BLUEPRINT.md, undoing
        # it can't need one either.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message='Revert "fix(loop): skip the user\'s own outbound DMs"',
            staged=["src/teatree/loop/slack_answer/cycle.py"],
            options=_RunOptions(is_revert_commit=True),
        )
        assert rc == 0

    def test_revert_prefix_message_exempt_without_revert_head(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A commit explicitly typed "revert:" (Conventional Commits' own
        # revert type) is exempt by prefix like "fix:" is, even when
        # REVERT_HEAD is absent — e.g. a revert commit replayed after a
        # rebase, outside a live `git revert` operation.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="revert: self-authored-DM filter drops all real inbound rows",
            staged=["src/teatree/loop/slack_answer/cycle.py"],
            options=_RunOptions(is_revert_commit=False),
        )
        assert rc == 0

    def test_non_revert_commit_with_unexempt_message_still_gated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The revert exemption must not over-correct: an ordinary commit
        # whose message happens to start with "Revert" is still gated when
        # it isn't actually mid-revert (REVERT_HEAD absent).
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="Revert my own local experiment",
            staged=["src/teatree/config/agent_spawn.py"],
            options=_RunOptions(is_revert_commit=False),
        )
        assert rc == 1


class TestIsShippedContract:
    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/config/defaults.toml",
            "src/teatree/config/enums.py",
            "src/teatree/config/agent_enums.py",
        ],
    )
    def test_documented_value_surfaces_count(self, path: str) -> None:
        assert hook._is_shipped_contract(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/config/settings.py",
            "src/teatree/config/defaults_approvals.toml",
            "src/teatree/core/models/enumerations.py",
            "BLUEPRINT.md",
        ],
    )
    def test_other_paths_do_not_count(self, path: str) -> None:
        assert hook._is_shipped_contract(path) is False

    def test_vendored_prefix_is_stripped(self) -> None:
        assert hook._is_shipped_contract("vendor/teatree/src/teatree/config/defaults.toml", "vendor/teatree/") is True


class TestShippedContractWithdrawsTypeExemption:
    """A `chore:` that flips a shipped default must not be exempt from the sync gate."""

    _run = TestMain._run

    def test_chore_flipping_a_shipped_default_is_gated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="chore(config): default autonomous posture",
            staged=["src/teatree/config/defaults.toml"],
        )
        assert rc == 1

    def test_fix_touching_an_enum_vocabulary_is_gated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="fix(config): drop a retired mode value",
            staged=["src/teatree/config/enums.py"],
        )
        assert rc == 1

    def test_correcting_the_prose_clears_it(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="chore(config): default autonomous posture",
            staged=["src/teatree/config/defaults.toml", "docs/blueprint/configuration.md"],
        )
        assert rc == 0

    def test_neighbouring_config_change_keeps_its_exemption(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="chore(config): rename a private helper",
            staged=["src/teatree/config/settings.py"],
        )
        assert rc == 0

    def test_merge_replay_stays_exempt(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="Merge branch 'main' into feature",
            staged=["src/teatree/config/defaults.toml"],
            options=_RunOptions(is_merge_commit=True),
        )
        assert rc == 0


FORK_OWN_SRC = "src/fork_overlay/sibling.py"
CORE_SRC = "src/teatree/thing.py"
_GIT_BIN = shutil.which("git") or "/usr/bin/git"


@dataclass(frozen=True)
class _Checkout:
    """A real git checkout plus where teatree's own tree sits inside it."""

    repo: Path
    source_root: Path
    script: Path

    def stage(self, *repo_relative: str) -> None:
        for path in repo_relative:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# edit {path}\n", encoding="utf-8")
        _git(self.repo, "add", *repo_relative)

    def run_hook(self, message: str, *, cwd: Path | None = None) -> int:
        msg_file = self.repo / ".git" / "COMMIT_EDITMSG"
        msg_file.write_text(message + "\n", encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(self.script), str(msg_file)],
            cwd=cwd or self.source_root,
            capture_output=True,
            text=True,
            check=False,
        ).returncode

    def commit(self, message: str, *only: str) -> int:
        """Commit through real git, so the hook runs as git actually invokes it."""
        return subprocess.run(
            [_GIT_BIN, "commit", "-q", "-m", message, *(["--", *only] if only else [])],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        ).returncode

    def head_subject(self) -> str:
        result = subprocess.run(
            [_GIT_BIN, "log", "-1", "--format=%s"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()


def _git(repo: Path, *args: str) -> None:
    subprocess.run([_GIT_BIN, *args], cwd=repo, capture_output=True, text=True, check=True)


def _install_commit_msg_hook(checkout: _Checkout) -> None:
    """Wire the script as git's own ``commit-msg`` hook, entered at teatree's root.

    That re-entry is prek's ``--cd`` venue, and it is where git's RELATIVE
    ``GIT_INDEX_FILE`` meets the hook's own resolution — an interaction no
    direct invocation of the script can reach.
    """
    offset = checkout.source_root.relative_to(checkout.repo).as_posix()
    git_hook = checkout.repo / ".git" / "hooks" / "commit-msg"
    git_hook.write_text(
        f'#!/bin/sh\ncd "{offset}" || exit 2\nexec {sys.executable} scripts/hooks/check_blueprint_sync.py "$@"\n',
        encoding="utf-8",
    )
    git_hook.chmod(0o755)


def _build_checkout(tmp_path: Path, *, vendored: bool) -> _Checkout:
    """A git checkout carrying the SHIPPED hook script at the layout under test.

    ``vendored`` puts teatree's tree under ``vendor/teatree/`` with a sibling
    ``src/fork_overlay/`` the fork owns — the layout where every path the hook
    reasons about arrives prefixed and the fork's own source is not teatree's.
    """
    repo = tmp_path / "repo"
    source_root = repo / "vendor" / "teatree" if vendored else repo
    hooks_dir = source_root / "scripts" / "hooks"
    hooks_dir.mkdir(parents=True)
    script = hooks_dir / "check_blueprint_sync.py"
    shutil.copy(Path(hook.__file__), script)

    for seed in ("BLUEPRINT.md", "docs/blueprint/configuration.md", CORE_SRC):
        seeded = source_root / seed
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(f"# {seed}\n", encoding="utf-8")
    if vendored:
        (repo / "src" / "fork_overlay").mkdir(parents=True)
        (repo / FORK_OWN_SRC).write_text("# overlay\n", encoding="utf-8")

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "hook@example.test")
    _git(repo, "config", "user.name", "hook")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    return _Checkout(repo=repo, source_root=source_root, script=script)


class TestStandaloneLayout:
    """A plain clone: teatree's root IS the repo root, so nothing is prefixed."""

    @pytest.fixture
    def checkout(self, tmp_path: Path) -> _Checkout:
        return _build_checkout(tmp_path, vendored=False)

    def test_src_without_blueprint_is_gated(self, checkout: _Checkout) -> None:
        checkout.stage("src/teatree/thing.py")
        assert checkout.run_hook("feat(core): a capability") == 1

    def test_src_with_blueprint_passes(self, checkout: _Checkout) -> None:
        checkout.stage("src/teatree/thing.py", "BLUEPRINT.md")
        assert checkout.run_hook("feat(core): a capability") == 0

    def test_src_with_appendix_passes(self, checkout: _Checkout) -> None:
        checkout.stage("src/teatree/thing.py", "docs/blueprint/configuration.md")
        assert checkout.run_hook("feat(core): a capability") == 0


class TestVendoredLayout:
    """A fork vendoring core: teatree's tree is ``vendor/teatree/``.

    Every staged path git reports is relative to the FORK root, so teatree's own
    source arrives as ``vendor/teatree/src/…`` and its BLUEPRINT as
    ``vendor/teatree/BLUEPRINT.md`` — while ``src/fork_overlay/`` belongs to the
    fork and is not teatree source at all.
    """

    @pytest.fixture
    def checkout(self, tmp_path: Path) -> _Checkout:
        return _build_checkout(tmp_path, vendored=True)

    def test_core_src_without_blueprint_is_gated(self, checkout: _Checkout) -> None:
        checkout.stage("vendor/teatree/src/teatree/thing.py")
        assert checkout.run_hook("feat(core): a capability") == 1

    def test_core_src_with_vendored_blueprint_passes(self, checkout: _Checkout) -> None:
        checkout.stage("vendor/teatree/src/teatree/thing.py", "vendor/teatree/BLUEPRINT.md")
        assert checkout.run_hook("feat(core): a capability") == 0

    def test_core_src_with_vendored_appendix_passes(self, checkout: _Checkout) -> None:
        checkout.stage("vendor/teatree/src/teatree/thing.py", "vendor/teatree/docs/blueprint/configuration.md")
        assert checkout.run_hook("feat(core): a capability") == 0

    def test_fork_own_src_is_not_teatree_source(self, checkout: _Checkout) -> None:
        checkout.stage(FORK_OWN_SRC)
        assert checkout.run_hook("feat(overlay): a capability") == 0

    @pytest.mark.parametrize(
        ("staged", "expected"),
        [
            (["vendor/teatree/src/teatree/thing.py"], 1),
            (["vendor/teatree/src/teatree/thing.py", "vendor/teatree/BLUEPRINT.md"], 0),
        ],
    )
    def test_verdict_is_unchanged_by_a_concurrent_agents_staged_file(
        self, checkout: _Checkout, staged: list[str], expected: int
    ) -> None:
        # A shared clone lets a sibling agent's staged work reach this hook's
        # index read. It must not be able to move the verdict.
        checkout.stage(*staged)
        alone = checkout.run_hook("feat(core): a capability")
        checkout.stage(FORK_OWN_SRC)
        assert (alone, checkout.run_hook("feat(core): a capability")) == (expected, expected)

    def test_verdict_is_unchanged_by_the_invoking_cwd(self, checkout: _Checkout) -> None:
        checkout.stage("vendor/teatree/src/teatree/thing.py")
        from_source_root = checkout.run_hook("feat(core): a capability")
        from_repo_root = checkout.run_hook("feat(core): a capability", cwd=checkout.repo)
        assert (from_source_root, from_repo_root) == (1, 1)


class TestUnderRealGitCommit:
    """The hook wired as git's own ``commit-msg``, entered at teatree's root.

    Git exports a RELATIVE ``GIT_INDEX_FILE`` to its hooks, so this is the only
    shape that proves the hook still reads the right index once it resolves paths
    from teatree's root rather than the process cwd — and the only one that shows
    a commit is genuinely refused rather than merely returning non-zero.
    """

    @pytest.fixture(params=[False, True], ids=["standalone", "vendored"])
    def checkout(self, request: pytest.FixtureRequest, tmp_path: Path) -> _Checkout:
        built = _build_checkout(tmp_path, vendored=request.param)
        _install_commit_msg_hook(built)
        return built

    def _core_src(self, checkout: _Checkout) -> str:
        return (checkout.source_root / CORE_SRC).relative_to(checkout.repo).as_posix()

    def _blueprint(self, checkout: _Checkout) -> str:
        return (checkout.source_root / "BLUEPRINT.md").relative_to(checkout.repo).as_posix()

    def test_core_src_without_blueprint_refuses_the_commit(self, checkout: _Checkout) -> None:
        checkout.stage(self._core_src(checkout))
        assert checkout.commit("feat(core): a capability") != 0
        assert checkout.head_subject() == "chore: seed"

    def test_core_src_with_blueprint_creates_the_commit(self, checkout: _Checkout) -> None:
        checkout.stage(self._core_src(checkout), self._blueprint(checkout))
        assert checkout.commit("feat(core): a capability") == 0
        assert checkout.head_subject() == "feat(core): a capability"

    def test_path_limited_commit_ignores_a_concurrent_agents_staged_file(self, checkout: _Checkout) -> None:
        # git points GIT_INDEX_FILE at a temporary index holding only the named
        # paths, so a sibling agent's staged work in a shared clone is neither
        # committed nor judged.
        core, blueprint = self._core_src(checkout), self._blueprint(checkout)
        checkout.stage(core, blueprint)
        checkout.stage("src/fork_overlay/sibling.py")
        assert checkout.commit("feat(core): a capability", core, blueprint) == 0
        assert checkout.head_subject() == "feat(core): a capability"
