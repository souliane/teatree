"""The Codex App Server child gets the routed forge token and the system git helper, and no other credential."""

import os
import shutil
import subprocess
from pathlib import Path

from teatree.agents.codex_app_server_env import codex_process_env

_SECRETS = {
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
    "GITLAB_TOKEN": "gitlab-secret",
    "T3_ADMIN_PASSWORD": "admin-secret",
    "NOTION_TOKEN": "notion-secret",
    "T3_AGENT_MAILBOX_TOKEN": "mailbox-secret",
    "GH_TOKEN": "ambient-gh-secret",
    "GITHUB_TOKEN": "ambient-github-secret",
}


def _env(home: Path, extra: dict[str, str] | None = None, *, path: str = "/usr/bin") -> dict[str, str]:
    return codex_process_env(home, extra, ambient={"PATH": path, "LANG": "C.UTF-8", **_SECRETS})


def test_the_routed_token_and_the_worker_cap_are_the_only_dispatch_keys_taken(tmp_path: Path) -> None:
    env = _env(tmp_path / "home", {**_SECRETS, "GH_TOKEN": "ghp-routed", "PYTEST_XDIST_AUTO_NUM_WORKERS": "3"})

    assert env["GH_TOKEN"] == "ghp-routed"
    assert env["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "3"
    assert not {key for key in _SECRETS if key != "GH_TOKEN"} & env.keys()
    assert "ghp-routed" not in {value for key, value in env.items() if key != "GH_TOKEN"}
    assert not {"ambient-gh-secret", "ambient-github-secret"} & set(env.values())


def test_a_dispatch_without_a_routed_token_gets_none(tmp_path: Path) -> None:
    assert "GH_TOKEN" not in _env(tmp_path / "home")


def test_the_system_gitconfig_and_the_credential_helper_are_left_alone(tmp_path: Path) -> None:
    env = _env(tmp_path / "home", {"GH_TOKEN": "ghp-routed"})

    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "home" / ".gitconfig")
    assert not {key for key in env if key.startswith("GIT_CONFIG_")} - {"GIT_CONFIG_GLOBAL"}


def test_git_answers_a_github_credential_request_with_the_routed_token(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text('#!/bin/sh\ncat >/dev/null\nprintf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"\n')
    fake_gh.chmod(0o755)
    system_config = tmp_path / "system.gitconfig"
    system_config.write_text('[credential "https://github.com"]\n\thelper =\n\thelper = !gh auth git-credential\n')
    env = _env(tmp_path / "home", {"GH_TOKEN": "ghp-routed"}, path=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    env.setdefault("GIT_CONFIG_SYSTEM", str(system_config))

    filled = subprocess.run(
        [shutil.which("git") or "git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        env={**env, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert "password=ghp-routed" in filled.stdout
