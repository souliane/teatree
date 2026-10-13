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


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ('bash -lc "git worktree add /outside/new"', Path("/outside/new")),
        ('sh -ec "git worktree add /outside/new"', Path("/outside/new")),
        ('eval "git worktree add /outside/new"', Path("/outside/new")),
        ('echo "git worktree add /outside/new" | bash', Path("/outside/new")),
        ('flock /tmp/lock -c "git worktree add /outside/new"', Path("/outside/new")),
        ("git worktree add --orphan /outside/new", Path("/outside/new")),
        ("git worktree add -qfb feature /outside/new", Path("/outside/new")),
        ("git worktree add -fb feature /outside/new", Path("/outside/new")),
        ("VAR=/outside git worktree add $VAR/new", Path("/outside/new")),
        ("D=$(mktemp -d /tmp/review.XXXX) && git worktree add $D/new", Path("/tmp/review.XXXX/new")),
        ('git worktree add "~/relative"', None),
        ("ls | xargs git worktree add", None),
    ],
)
def test_shell_checkout_variants(command: str, target: Path | None, tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(command, tmp_path)
    assert len(calls) == 1
    if command == 'git worktree add "~/relative"':
        assert calls[0].target == tmp_path / "~" / "relative"
    else:
        assert calls[0].target == target


def test_bare_mktemp_assignment_has_unknown_target(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("D=$(mktemp -d) && git worktree add $D/new", tmp_path)
    assert len(calls) == 1
    assert calls[0].target is None


def test_mktemp_assignment_inside_quoted_shell_script_is_scanned(tmp_path: Path) -> None:
    command = "bash -c 'D=$(mktemp -d /tmp/review.XXXX); git worktree add $D/new'"
    calls = scan_worktree_checkouts(command, tmp_path)
    assert len(calls) == 1
    assert calls[0].target == Path("/tmp/review.XXXX/new")


def test_git_dir_selects_source_but_relative_target_uses_cwd(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts(f"git --git-dir={tmp_path / 'clone' / '.git'} worktree add target", tmp_path)
    assert calls[0].source == tmp_path / "clone"
    assert calls[0].target == tmp_path / "target"


def test_env_chdir_sets_source_and_target_base(tmp_path: Path) -> None:
    calls = scan_worktree_checkouts("env -C clone git worktree add ../target", tmp_path)
    assert calls[0].source == tmp_path / "clone"
    assert calls[0].target == tmp_path / "target"


@pytest.mark.parametrize(
    "command", ['bash -c "echo $HOME"', "timeout 1800 bash -c 'until [ -f \"$marker\" ]; do sleep 20; done'"]
)
def test_ordinary_dollar_bearing_shell_script_has_no_checkout(command: str, tmp_path: Path) -> None:
    assert scan_worktree_checkouts(command, tmp_path) == []
