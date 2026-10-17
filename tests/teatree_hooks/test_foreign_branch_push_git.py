"""The ownership probe reads remote refs outside the checkout."""

import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from hooks.scripts.foreign_branch_push_git import remote_branch_oid
from teatree.forge_credentials import scrub_active_tokens


def _git(work: Path, *args: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(
        [executable, "-C", str(work), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_scene(tmp_path: Path) -> tuple[Path, Path, str]:
    work, remote = tmp_path / "work", tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    (work / "README").write_text("example", encoding="utf-8")
    for args in (
        ("add", "README"),
        ("-c", "user.name=Example", "-c", "user.email=e@example.test", "commit", "-qm", "seed"),
        ("remote", "add", "origin", str(remote)),
        ("push", "-q", "origin", "main"),
    ):
        _git(work, *args)
    oid = _git(work, "rev-parse", "HEAD")
    return work, remote, oid


def test_file_remote_ignores_checkout_uploadpack(git_scene: tuple[Path, Path, str], tmp_path: Path) -> None:
    work, _, oid = git_scene
    marker = tmp_path / "uploadpack-ran"
    program = tmp_path / "uploadpack"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec git-upload-pack \"$@\"\n", encoding="utf-8")
    program.chmod(0o755)
    _git(work, "config", "--local", "remote.origin.uploadpack", str(program))

    probe = remote_branch_oid(str(work), "origin", "main")

    assert probe.ok
    assert probe.out == oid
    assert not marker.exists()


def test_ssh_remote_uses_system_command_and_ignores_checkout_command(
    git_scene: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, remote, oid = git_scene
    local_marker = tmp_path / "local-ssh-ran"
    system_ssh = tmp_path / "system-ssh"
    system_ssh.write_text('#!/bin/sh\nexec git-upload-pack "$T3_TEST_REMOTE_PATH"\n', encoding="utf-8")
    system_ssh.chmod(0o755)
    system_config = tmp_path / "system.gitconfig"
    system_config.write_text(f"[core]\n\tsshCommand = {system_ssh}\n", encoding="utf-8")
    _git(work, "remote", "set-url", "origin", "ssh://example.test/repo.git")
    _git(work, "config", "--local", "core.sshCommand", f"touch {local_marker}")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("T3_TEST_REMOTE_PATH", str(remote))

    probe = remote_branch_oid(str(work), "origin", "main")

    assert probe.ok
    assert probe.out == oid
    assert not local_marker.exists()


def test_url_scoped_system_helper_is_consulted_without_checkout_helper(
    git_scene: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _, _ = git_scene

    class Challenge(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.end_headers()

        def log_message(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    server = HTTPServer(("127.0.0.1", 0), Challenge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/repo.git"
        system_marker, local_marker = tmp_path / "system-helper-ran", tmp_path / "local-helper-ran"
        helper = tmp_path / "system-helper"
        helper.write_text(f"#!/bin/sh\ntouch '{system_marker}'\nprintf 'username=sentinel\\n'\n", encoding="utf-8")
        helper.chmod(0o755)
        local_helper = tmp_path / "local-helper"
        local_helper.write_text(f"#!/bin/sh\ntouch '{local_marker}'\nprintf 'password=sentinel\\n'\n", encoding="utf-8")
        local_helper.chmod(0o755)
        config = tmp_path / "system.gitconfig"
        config.write_text(f'[credential "{url}"]\n\thelper = {helper}\n', encoding="utf-8")
        _git(work, "remote", "set-url", "origin", url)
        _git(work, "config", "--local", "credential.helper", str(local_helper))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

        assert not remote_branch_oid(str(work), "origin", "main").ok
        assert system_marker.exists()
        assert not local_marker.exists()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_probe_argv_uses_remote_url_and_neutral_directory(tmp_path: Path) -> None:
    oid = "a" * 40
    with patch(
        "hooks.scripts.foreign_branch_push_git.subprocess.run",
        side_effect=[
            CompletedProcess(["git"], 0, "file:///remote.git\n", ""),
            CompletedProcess(["git"], 0, f"{oid}\trefs/heads/main\n", ""),
        ],
    ) as run:
        probe = remote_branch_oid(str(tmp_path), "origin", "main")

    assert probe.out == oid
    config_argv = run.call_args_list[0].args[0]
    ls_argv = run.call_args_list[1].args[0]
    assert config_argv[-3:] == ["config", "--get", "remote.origin.url"]
    assert ls_argv[-4:] == ["ls-remote", "--heads", "file:///remote.git", "refs/heads/main"]
    assert ls_argv[2] != str(tmp_path)
    assert "-c" not in ls_argv


@pytest.mark.parametrize("value", ["", "x", "short"])
def test_short_environment_values_do_not_scrub_unrelated_text(value: str) -> None:
    assert scrub_active_tokens("example", {"GH_TOKEN": value}) == "example"


def test_long_environment_sentinel_is_scrubbed() -> None:
    assert scrub_active_tokens("sentinel-long", {"GH_TOKEN": "sentinel-long"}) == "<redacted>"
