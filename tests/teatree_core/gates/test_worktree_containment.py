"""Static shell parsing for checkout placement decisions."""

from pathlib import Path

import pytest

from teatree.core.gates.worktree_containment import scan_worktree_checkouts


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("git worktree add /outside/new main", "/outside/new"),
        ("git -C nested -C ../clone worktree add -b feature /outside/new origin/main", "/outside/new"),
        ("git worktree move old /outside/new", "/outside/new"),
        ("env -i timeout 3 nice -n 1 git worktree add /outside/new main", "/outside/new"),
        ("flock /tmp/lock git worktree add /outside/new main", "/outside/new"),
        ("xargs -n 1 git worktree add /outside/new", "/outside/new"),
        ('target=/outside/new; git worktree add "$target" main', "/outside/new"),
        ('for target in /inside /outside/new; do git worktree add "$target"; done', "/outside/new"),
    ],
)
def test_finds_executed_checkout_target(command: str, target: str, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(command, tmp_path)
    assert any(call.target == Path(target) for call in calls)


@pytest.mark.parametrize(
    "command",
    [
        "echo 'git worktree add /outside/new main'",
        "cat <<'EOF'\ngit worktree add /outside/new main\nEOF",
        "git worktree list",
        "git worktree remove /outside/old",
    ],
)
def test_ignores_non_checkout_text(command: str, tmp_path: Path) -> None:
    assert scan_worktree_checkouts(command, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        'git worktree add "$unknown" main',
        'git worktree add "$(mktemp -d)" main',
        'sh -c "$unknown"',
    ],
)
def test_unknown_destination_is_explicit(command: str, tmp_path: Path) -> None:
    assert any(call.target is None for call in scan_worktree_checkouts(command, tmp_path))


def test_literal_mktemp_directory_is_resolved(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts('git worktree add "$(mktemp -d /tmp/review.XXXXXX)" main', tmp_path)
    assert calls[0].target == Path("/tmp/review.XXXXXX")


def test_cd_and_git_dash_c_choose_source_and_relative_destination(tmp_path: Path) -> None:
    source = tmp_path / "clone"
    source.mkdir()
    command = f"cd {source} && git -C . worktree add ../outside main"
    calls = scan_worktree_checkouts(command, tmp_path)
    assert calls[0].source == source
    assert calls[0].target == tmp_path / "outside"
