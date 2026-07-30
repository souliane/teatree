"""Real-git integration tests for the git-hook installation probe."""

from pathlib import Path

import pytest

from teatree.core.gates.git_hooks_preflight import (
    GATES_BY_HOOK,
    PREK_CONFIG_NAME,
    REQUIRED_HOOK_NAMES,
    format_remediation,
    probe_checkouts,
    probe_git_hooks,
)
from tests._git_repo import make_git_repo, run_git


def install_hooks(repo: Path, *names: str) -> Path:
    """Write executable stand-ins for *names* (default: every required hook)."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in names or REQUIRED_HOOK_NAMES:
        hook = hooks_dir / name
        hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
    return hooks_dir


def make_prek_checkout(path: Path) -> Path:
    repo = make_git_repo(path)
    (repo / PREK_CONFIG_NAME).write_text("repos: []\n", encoding="utf-8")
    return repo


class TestProbeGitHooks:
    def test_fresh_repo_reports_both_hooks_missing(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")

        probe = probe_git_hooks(repo)

        assert probe.missing == REQUIRED_HOOK_NAMES
        assert not probe.ok
        assert probe.installable

    def test_sample_hooks_do_not_count_as_installed(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        sample = repo / ".git" / "hooks" / "pre-push.sample"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        sample.chmod(0o755)

        assert "pre-push" in probe_git_hooks(repo).missing

    def test_non_executable_hook_counts_as_missing(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        install_hooks(repo)
        (repo / ".git" / "hooks" / "pre-commit").chmod(0o644)

        assert probe_git_hooks(repo).missing == ("pre-commit",)

    def test_installed_hooks_pass(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        hooks_dir = install_hooks(repo)

        probe = probe_git_hooks(repo)

        assert probe.ok
        assert probe.missing == ()
        assert probe.hooks_dir == hooks_dir.resolve()

    def test_worktree_inherits_the_common_dir_verdict(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        worktree = tmp_path / "wt"
        run_git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))

        assert probe_git_hooks(worktree).missing == REQUIRED_HOOK_NAMES

        install_hooks(repo)

        assert probe_git_hooks(worktree).ok

    def test_deliberate_hooks_path_is_reported_not_judged(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        elsewhere = tmp_path / "operator-hooks"
        elsewhere.mkdir()
        run_git(repo, "config", "core.hooksPath", str(elsewhere))

        probe = probe_git_hooks(repo)

        assert probe.custom_hooks_path == str(elsewhere)
        assert probe.missing == ()
        assert not probe.installable

    def test_hooks_path_pointing_at_the_default_dir_is_not_custom(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        install_hooks(repo)
        run_git(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks"))

        probe = probe_git_hooks(repo)

        assert probe.custom_hooks_path is None
        assert probe.ok

    def test_non_repo_is_indeterminate_not_missing(self, tmp_path: Path) -> None:
        probe = probe_git_hooks(tmp_path)

        assert probe.indeterminate_reason is not None
        assert probe.missing == ()
        assert not probe.ok


class TestProbeCheckouts:
    def test_a_protected_clone_never_vouches_for_an_unprotected_one(self, tmp_path: Path) -> None:
        """The whole bug: hooks installed in one clone, absent in another."""
        container = make_prek_checkout(tmp_path / "container-clone")
        install_hooks(container)
        host = make_prek_checkout(tmp_path / "host-checkout")

        probes = probe_checkouts([container, host])

        assert [probe.checkout for probe in probes] == [container, host]
        assert probes[0].ok
        assert not probes[1].ok
        assert probes[1].missing == REQUIRED_HOOK_NAMES

    def test_worktrees_collapse_onto_their_clone_verdict(self, tmp_path: Path) -> None:
        clone = make_prek_checkout(tmp_path / "clone")
        first = tmp_path / "wt-a"
        second = tmp_path / "wt-b"
        for path, branch in ((first, "a"), (second, "b")):
            run_git(clone, "worktree", "add", "-q", "-b", branch, str(path))

        probes = probe_checkouts([clone, first, second])

        assert [probe.checkout for probe in probes] == [clone]

    def test_checkout_without_a_prek_config_is_skipped(self, tmp_path: Path) -> None:
        plain = make_git_repo(tmp_path / "plain")

        assert probe_checkouts([plain]) == []


class TestFormatRemediation:
    def test_names_the_checkout_each_missing_hook_and_the_install_command(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")

        text = "\n".join(format_remediation(probe_git_hooks(repo)))

        assert str(repo) in text
        assert "pre-commit" in text
        assert "pre-push" in text
        assert "t3 setup" in text

    def test_installed_repo_needs_no_remediation(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        install_hooks(repo)

        assert format_remediation(probe_git_hooks(repo)) == []


@pytest.mark.parametrize("name", REQUIRED_HOOK_NAMES)
def test_each_required_hook_documents_the_gates_it_carries(name: str) -> None:
    assert GATES_BY_HOOK[name]
