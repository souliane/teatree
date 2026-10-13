"""The Bash PreToolUse gate refuses new linked checkouts outside approved roots."""

# test-path: cross-cutting

from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.scripts.checkout_placement_gate import handle_block_worktree_checkout
from hooks.scripts.hook_router import _HANDLERS
from teatree.utils import git


@pytest.fixture
def managed_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    clone = tmp_path / "clone"
    clone.mkdir()
    git.run_strict(repo=str(clone), args=["init", "-q"])
    home = tmp_path / "home"
    monkeypatch.setenv("TEATREE_DISPOSABLE_CHECKOUT_ROOTS", "/tmp/allowed-temp")
    return clone, home


def _event(command: str, clone: Path) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(clone)}


def test_router_registers_gate() -> None:
    assert handle_block_worktree_checkout in _HANDLERS["PreToolUse"]


def test_denies_outside_target_and_names_remedies(managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    target = clone.parent / "stray"
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True) as deny,
    ):
        assert handle_block_worktree_checkout(_event(f"git worktree add {target} main", clone)) is True
    reason = deny.call_args.args[0]
    assert str(target) in reason
    assert str(home / "workspace" / "t3-workspaces") in reason
    assert "workspace_dir" in reason
    assert "temp root" in reason


def test_allows_base_temp_harness_and_unmanaged(managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    targets = [
        home / "workspace" / "t3-workspaces" / "ticket",
        Path("/tmp/allowed-temp/review"),
        clone / ".claude" / "worktrees" / "agent",
    ]
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
    ):
        for target in targets:
            assert handle_block_worktree_checkout(_event(f"git worktree add '{target}' main", clone)) is False
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=False),
    ):
        assert handle_block_worktree_checkout(_event(f"git worktree add {clone.parent / 'stray'} main", clone)) is False


def test_waiver_needs_reason_and_switch_disables_gate(managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    command = f"git worktree add {clone.parent / 'stray'} main"
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        assert handle_block_worktree_checkout(_event(command + " # [worktree-containment-ok: ]", clone)) is True
        assert (
            handle_block_worktree_checkout(_event(command + " # [worktree-containment-ok: reviewed]", clone)) is False
        )
        with patch("hooks.scripts.checkout_placement_gate._gate_enabled", return_value=False):
            assert handle_block_worktree_checkout(_event(command, clone)) is False


@pytest.mark.parametrize(
    ("command", "verdict"),
    [
        ("cd {clone} && git worktree add {outside} main", True),
        ("git -C {clone} -C . worktree add {outside} main", True),
        ("git worktree move old {outside}", True),
        ("env -i timeout 3 nice -n 1 git worktree add {outside} main", True),
        ("flock /tmp/lock git worktree add {outside} main", True),
        ("xargs -n 1 git worktree add {outside}", True),
        ('git worktree add "$unknown" main', True),
        ('git worktree add "$(mktemp -d)" main', True),
        ('for target in {base}/one {outside}; do git worktree add "$target"; done', True),
        ('sh -c "$unknown"', True),
        ('target={base}/one; git worktree add "$target" main', False),
        ('for target in {base}/one {base}/two; do git worktree add "$target"; done', False),
        ('git worktree add "$(mktemp -d /tmp/allowed-temp/review.XXXXXX)" main', False),
        ("git worktree add '{base}/with spaces' main", False),
        ("echo 'git worktree add {outside} main'", False),
        ("cat <<'EOF'\ngit worktree add {outside} main\nEOF", False),
        ("git worktree list", False),
    ],
)
def test_command_matrix(command: str, verdict: object, managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    base = home / "workspace" / "t3-workspaces"
    outside = clone.parent / "stray"
    call = command.format(clone=clone, base=base, outside=outside)
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        assert handle_block_worktree_checkout(_event(call, clone)) is verdict


def test_symlink_out_of_allowed_base_is_refused(managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    base = home / "workspace" / "t3-workspaces"
    base.mkdir(parents=True)
    (base / "alias").symlink_to(clone.parent, target_is_directory=True)
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        assert handle_block_worktree_checkout(_event(f"git worktree add {base / 'alias' / 'stray'} main", clone))
