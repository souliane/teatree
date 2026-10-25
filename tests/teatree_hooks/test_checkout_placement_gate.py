"""The Bash PreToolUse gate refuses new linked checkouts outside approved roots."""

# test-path: cross-cutting

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.scripts.checkout_placement_gate import handle_block_worktree_checkout
from hooks.scripts.hook_router import _HANDLERS
from teatree.config.checkout_roots import CheckoutRoots
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


@pytest.mark.parametrize("case", ["direct", "assigned", "cd_after_mktemp"])
def test_live_run_hook_denies_with_django_settings_exported(case: str) -> None:
    root = Path(__file__).resolve().parents[2]
    target = Path.home() / "stray-probe-5247-subprocess"
    command = {
        "direct": f"git worktree add {target} main",
        "assigned": "WT=$HOME/stray-probe-5247-subprocess; git worktree add $WT main",
        "cd_after_mktemp": f"X=$(mktemp -d); cd {root} && git worktree add {target} main",
    }[case]
    cwd = root.parent if case == "cd_after_mktemp" else root
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="teatree.settings", T3_HOOK_PYTHON=sys.executable)
    result = subprocess.run(
        [str(root / "hooks/scripts/run-hook.sh"), str(root / "hooks/scripts/hook_router.py"), "--event", "PreToolUse"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert "AppRegistryNotReady" not in result.stderr
    assert str(target) in result.stdout
    assert "deny" in result.stdout


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


def test_allows_base_temp_and_unmanaged_but_refuses_raw_harness(managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    targets = [
        home / "workspace" / "t3-workspaces" / "ticket",
        Path("/tmp/allowed-temp/review"),
    ]
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        for target in targets:
            assert handle_block_worktree_checkout(_event(f"git worktree add '{target}' main", clone)) is False
        harness_target = clone / ".claude" / "worktrees" / "agent"
        assert handle_block_worktree_checkout(_event(f"git worktree add {harness_target} main", clone)) is True
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=False),
    ):
        assert handle_block_worktree_checkout(_event(f"git worktree add {clone.parent / 'stray'} main", clone)) is False


def test_allows_home_and_tmpdir_spelling_of_allowed_targets(
    managed_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    clone, home = managed_clone
    monkeypatch.setenv("TMPDIR", "/tmp/allowed-temp")
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
    ):
        assert (
            handle_block_worktree_checkout(_event("git worktree add $HOME/workspace/t3-workspaces/one", clone)) is False
        )
        assert handle_block_worktree_checkout(_event("git worktree add $TMPDIR/two", clone)) is False


def test_ordinary_shell_script_is_allowed(managed_clone: tuple[Path, Path]) -> None:
    clone, _ = managed_clone
    with (
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        assert handle_block_worktree_checkout(_event('bash -c "echo $HOME"', clone)) is False


def test_reads_roots_once_for_multiple_checkout_calls(managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    base = home / "workspace" / "t3-workspaces"
    roots = CheckoutRoots(durable=(base,), disposable=(), harness=())
    command = f"git worktree add {base}/one; git worktree add {base}/two"
    with (
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("teatree.config.checkout_roots.checkout_roots", return_value=roots) as read_roots,
    ):
        assert handle_block_worktree_checkout(_event(command, clone)) is False
    read_roots.assert_called_once_with()


@pytest.mark.parametrize("redirect", ["2>&1", "2>/dev/null", "&>/dev/null", "> /dev/null"])
def test_move_from_an_allowed_cwd_still_checks_the_destination(redirect: str, tmp_path: Path) -> None:
    home = tmp_path / "home"
    clone = home / "workspace" / "t3-workspaces" / "clone"
    clone.mkdir(parents=True)
    git.run_strict(repo=str(clone), args=["init", "-q"])
    outside = tmp_path / "outside"
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        assert handle_block_worktree_checkout(_event(f"git worktree move old {outside} {redirect}", clone)) is True


def test_waiver_needs_reason_and_switch_disables_gate(managed_clone: tuple[Path, Path]) -> None:
    clone, home = managed_clone
    command = f"git worktree add {clone.parent / 'stray'} main"
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router._gate_escape.escape_allows", side_effect=[False, False, True]),
    ):
        assert handle_block_worktree_checkout(_event(command + " # [worktree-containment-ok: reviewed]", clone)) is True
        assert (
            handle_block_worktree_checkout(_event(command + " # [gate-escape:worktree_containment: ]", clone)) is True
        )
        assert (
            handle_block_worktree_checkout(_event(command + " # [gate-escape:worktree_containment: reviewed]", clone))
            is False
        )
        with patch("hooks.scripts.checkout_placement_gate._gate_enabled", return_value=False):
            assert handle_block_worktree_checkout(_event(command, clone)) is False


@pytest.mark.parametrize(
    ("command", "verdict"),
    [
        ("cd {clone} && git worktree add {outside} main", True),
        ("git -C {clone} -C . worktree add {outside} main", True),
        ("git worktree move old {outside}", True),
        ("git worktree move old {outside} 2>&1", True),
        ("git worktree move old {outside} > /dev/null", True),
        ("cd {clone} 2>/dev/null && git worktree add {outside} main", True),
        ("pushd {clone} >/dev/null && git worktree add {outside} main", True),
        ("bash -c -e 'git worktree add {outside} main'", True),
        ("sudo git worktree add {outside} main", True),
        ("bash <<EOF\ngit worktree add {outside} main\nEOF", True),
        ("git worktree add -bdb {outside} HEAD", True),
        ('git worktree add "$(mktemp -d --tmpdir={outside} wt.XXXX)" main', True),
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
        ("D=$(mktemp -d /tmp/allowed-temp/review.XXXXXX) && git -C {clone} worktree add $D/wt", False),
        ("D=$(mktemp -d) && git -C {clone} worktree add $D/wt", True),
        ("ls | xargs git -C {clone} worktree add", True),
        ('bash -lc "git -C {clone} worktree add {outside}"', True),
        ('bash -c "echo $HOME"', False),
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


@pytest.mark.parametrize("command", ["ls -la", "git status && git log --oneline | head -3", "uv run pytest -q"])
def test_a_command_that_cannot_add_a_worktree_never_loads_the_scanner(command: str, tmp_path: Path) -> None:
    with patch("hooks.scripts.checkout_placement_gate.import_module") as loader:
        assert handle_block_worktree_checkout(_event(command, tmp_path)) is False
    loader.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        'git -C {clone} work"tree" add {outside} main',
        "git -C {clone} $'\\x77orktree' add {outside} main",
        "(cd {clone} && git worktree add {outside} main)",
    ],
)
def test_split_spellings_and_subshells_from_an_unmanaged_cwd_are_denied(
    command: str, managed_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    clone, home = managed_clone
    unmanaged = tmp_path / "elsewhere"
    unmanaged.mkdir()
    call = command.format(clone=clone, outside=clone.parent / "stray")
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        assert handle_block_worktree_checkout(_event(call, unmanaged)) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo {outside} | xargs -I % git worktree add % main",
        "echo {outside} | xargs -I {{}} git worktree add {{}} main",
        "echo {outside} | xargs -I % bash -c 'git worktree add % main'",
        "parallel git worktree add {{}} main ::: {outside}",
        "git worktree add {{{outside},x}} main",
        "true && bash <<EOF\ngit worktree add {outside} main\nEOF",
        "cat <<EOF | bash\ngit worktree add {outside} main\nEOF",
        "/bin/bash <<'EOF'\ngit worktree add {outside} main\nEOF",
        "sudo bash <<EOF\ngit worktree add {outside} main\nEOF",
        "bash <<<'git worktree add {outside} main'",
        "sudo -D {clone} git worktree add ../stray main",
        "parallel --wd {clone} git worktree add ../stray main ::: 1",
    ],
)
def test_substituted_and_shell_fed_targets_from_an_allowed_cwd_are_denied(
    command: str, managed_clone: tuple[Path, Path]
) -> None:
    clone, home = managed_clone
    ticket_checkout = home / "workspace" / "t3-workspaces" / "5247-ticket" / "repo"
    ticket_checkout.mkdir(parents=True)
    git.run_strict(repo=str(ticket_checkout), args=["init", "-q"])
    call = command.format(clone=clone, outside=clone.parent / "stray")
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("hooks.scripts.checkout_placement_gate.repo_root_is_teatree_managed", return_value=True),
        patch("hooks.scripts.hook_router.emit_pretooluse_deny", return_value=True),
    ):
        assert handle_block_worktree_checkout(_event(call, ticket_checkout)) is True
        assert (
            handle_block_worktree_checkout(_event(f"git worktree add {ticket_checkout.parent / 'b'}", clone)) is False
        )
