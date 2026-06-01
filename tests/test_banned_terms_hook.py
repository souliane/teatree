import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree import find_project_root
from teatree.hooks import _repo_visibility


@pytest.mark.integration
def test_banned_terms_hook_expands_tilde_config_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / ".teatree.toml"
    config.write_text('[teatree]\nbanned_terms = ["acme"]\n', encoding="utf-8")

    sample = tmp_path / "README.md"
    sample.write_text("acme overlay\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        [str(script), "--config", "~/.teatree.toml", str(sample)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert "BANNED TERM" in result.stdout


@pytest.mark.integration
def test_banned_terms_hook_ignores_matches_inside_email_addresses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / ".teatree.toml"
    config.write_text('[teatree]\nbanned_terms = ["internalterm"]\n', encoding="utf-8")

    sample = tmp_path / "AGENTS.md"
    sample.write_text("Git author: adrien <adrien.cossa@internalterm.example>\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        [str(script), "--config", "~/.teatree.toml", str(sample)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0


def _git(cwd: Path, *args: str) -> None:
    git_bin = shutil.which("git")
    assert git_bin is not None
    subprocess.run(
        [git_bin, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


@pytest.mark.integration
def test_banned_terms_block_emits_visibility_unknown_note_and_still_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A private-LOOKING target whose visibility is UNKNOWN in-hook (no probe
    # tool resolvable, not in the allowlist) must STILL hard-block, and emit a
    # diagnostic stderr NOTE pointing the operator at [teatree] private_repos.
    home = Path(os.environ["HOME"])  # the conftest-isolated HOME
    (home / ".teatree.toml").write_text('[teatree]\nbanned_terms = ["acmewidget"]\n', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/secret-product.git")

    # The probe tool is unreachable in-hook -> visibility "unknown" -> the
    # block stands. Patching the resolver (not PATH) keeps the shell scanner's
    # ``bash``/``grep`` reachable so the banned-term match still fires.
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --body "rolling out acmewidget"'},
        "cwd": str(repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is True
    decision = json.loads(captured.out)
    assert decision["permissionDecision"] == "deny"
    assert "banned-terms" in decision["permissionDecisionReason"]
    assert "acme/secret-product" in captured.err
    assert "private_repos" in captured.err


def test_banned_terms_block_no_note_when_target_resolvable_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # When the target IS allowlisted-private the carve-out downgrades (no deny,
    # no unknown NOTE) -- the hint only fires on a genuine unknown-target block.
    home = Path(os.environ["HOME"])  # the conftest-isolated HOME
    (home / ".teatree.toml").write_text(
        '[teatree]\nbanned_terms = ["acmewidget"]\nprivate_repos = ["acme/secret-product"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/secret-product.git")
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)
    monkeypatch.delenv("GH_REPO", raising=False)

    data = {
        "tool_name": "Bash",
        "tool_input": {"command": 'gh issue create --body "rolling out acmewidget"'},
        "cwd": str(repo),
    }
    blocked = handle_banned_terms_pretool(data)
    captured = capsys.readouterr()

    assert blocked is False
    assert "downgraded to warn" in captured.err
    assert "visibility unknown" not in captured.err
