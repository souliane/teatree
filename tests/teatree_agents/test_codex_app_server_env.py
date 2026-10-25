"""The Codex App Server child gets the routed forge token and the system git helper, and no other credential."""

import os
import shutil
import subprocess
from pathlib import Path

from teatree.agents.codex_app_server_env import codex_process_env, router_process_env
from teatree.utils.agent_spawn_marker import DISPATCHED_AGENT_ENV

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
    shared_root = tmp_path / "shared-pool"
    env = _env(
        tmp_path / "home",
        {
            **_SECRETS,
            "GH_TOKEN": "ghp-routed",
            "PYTEST_XDIST_AUTO_NUM_WORKERS": "3",
            "TEATREE_PYTEST_WORKER_POOL_ROOT": str(shared_root),
            "T3_LOOP_REGISTRY_DIR": str(tmp_path / "loop-owner"),
        },
    )

    assert env["GH_TOKEN"] == "ghp-routed"
    assert env["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "3"
    assert env["TEATREE_PYTEST_WORKER_POOL_ROOT"] == str(shared_root)
    assert "T3_LOOP_REGISTRY_DIR" not in env
    assert env["T3_DISPATCHED_AGENT"] == "1"
    assert not {key for key in _SECRETS if key != "GH_TOKEN"} & env.keys()
    assert "ghp-routed" not in {value for key, value in env.items() if key != "GH_TOKEN"}
    assert not {"ambient-gh-secret", "ambient-github-secret"} & set(env.values())


def test_ambient_pool_root_reaches_codex_without_loop_registry(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared-pool"
    env = codex_process_env(
        tmp_path / "private-home",
        ambient={
            "PATH": "/usr/bin",
            "TEATREE_PYTEST_WORKER_POOL_ROOT": str(shared_root),
            "T3_LOOP_REGISTRY_DIR": str(tmp_path / "loop-owner"),
        },
    )

    assert env["TEATREE_PYTEST_WORKER_POOL_ROOT"] == str(shared_root)
    assert "T3_LOOP_REGISTRY_DIR" not in env


def test_pool_root_does_not_expand_router_allowlist(tmp_path: Path) -> None:
    ambient = {
        "PATH": "/usr/bin",
        "TEATREE_PYTEST_WORKER_POOL_ROOT": str(tmp_path / "shared-pool"),
        "T3_LOOP_REGISTRY_DIR": str(tmp_path / "loop-owner"),
    }
    dispatch = {
        "GH_TOKEN": "ghp-routed",
        "TEATREE_PYTEST_WORKER_POOL_ROOT": ambient["TEATREE_PYTEST_WORKER_POOL_ROOT"],
        "T3_LOOP_REGISTRY_DIR": ambient["T3_LOOP_REGISTRY_DIR"],
    }

    router_env = router_process_env(dispatch, ambient=ambient)

    assert router_env == {"PATH": "/usr/bin", "GH_TOKEN": "ghp-routed"}


def test_a_dispatch_without_a_routed_token_gets_none(tmp_path: Path) -> None:
    assert "GH_TOKEN" not in _env(tmp_path / "home")


def test_router_keeps_worker_hook_paths_without_inheriting_other_credentials() -> None:
    hook_env = {
        "T3_DATA_DIR": "/tmp/t3-data",
        "XDG_DATA_HOME": "/tmp/xdg-data",
        "UV_TOOL_DIR": "/tmp/uv-tools",
        "T3_HOOK_PYTHON": "/tmp/python",
        "T3_HOOK_STATE_DIR": "/tmp/hook-state",
        "T3_SKILL_SEARCH_DIRS": "/tmp/skills",
        "TEATREE_TERM_REGISTRY": "test-registry",
    }

    env = router_process_env({}, ambient={"PATH": "/usr/bin", **hook_env, "T3_CONTROL_DB_DIR": "/control", **_SECRETS})

    assert {key: env[key] for key in hook_env} == hook_env
    assert "T3_CONTROL_DB_DIR" not in env
    assert not _SECRETS.keys() & env.keys()


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


def test_every_codex_process_is_marked_as_a_dispatched_agent(tmp_path: Path) -> None:
    assert _env(tmp_path / "home")[DISPATCHED_AGENT_ENV] == "1"
