# test-path: cross-cutting — drives hooks/scripts/foreign_branch_push_git.py and the gate; no src/teatree/ mirror.
"""The ownership probe reads remote refs outside the checkout."""

import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

import hooks.scripts.foreign_branch_push_git as git_probes
from hooks.scripts.foreign_branch_push_argv import PushSpec
from hooks.scripts.foreign_branch_push_gate import _branch_refusal, _ForgeRun, _venue_fault
from hooks.scripts.foreign_branch_push_git import GitProbe, live_remote_range, remote_branch_oid
from teatree.forge_credentials import CODEX_ROUTER_ENV, FORGE_TOKEN_ENV_KEYS


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


@pytest.fixture
def challenge_url() -> Iterator[str]:
    """An HTTP remote that answers every request with a 401 Basic challenge."""

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
        yield f"http://127.0.0.1:{server.server_port}/repo.git"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


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
    _git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    probe = remote_branch_oid(str(work), "origin", "main")
    live = live_remote_range(str(work), "origin", "main", oid)

    assert probe.ok
    assert probe.out == oid
    assert not live.failed
    assert not local_marker.exists()


def test_url_scoped_system_helper_is_consulted_without_checkout_helper(
    git_scene: tuple[Path, Path, str], challenge_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _, _ = git_scene
    system_marker, local_marker = tmp_path / "system-helper-ran", tmp_path / "local-helper-ran"
    helper = tmp_path / "system-helper"
    helper.write_text(f"#!/bin/sh\ntouch '{system_marker}'\nprintf 'username=sentinel\\n'\n", encoding="utf-8")
    helper.chmod(0o755)
    local_helper = tmp_path / "local-helper"
    local_helper.write_text(f"#!/bin/sh\ntouch '{local_marker}'\nprintf 'password=sentinel\\n'\n", encoding="utf-8")
    local_helper.chmod(0o755)
    config = tmp_path / "system.gitconfig"
    config.write_text(f'[credential "{challenge_url}"]\n\thelper = {helper}\n', encoding="utf-8")
    _git(work, "remote", "set-url", "origin", challenge_url)
    _git(work, "config", "--local", "credential.helper", str(local_helper))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    assert not remote_branch_oid(str(work), "origin", "main").ok
    assert system_marker.exists()
    assert not local_marker.exists()


@pytest.mark.parametrize(
    "helper_answer",
    ["", "username=sentinel\\npassword=sentinel\\n"],
    ids=["no-credential", "rejected-credential"],
)
def test_non_github_credential_refusal_keeps_general_gate_refusal(
    git_scene: tuple[Path, Path, str],
    challenge_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_answer: str,
) -> None:
    work, _, _ = git_scene
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\nprintf '{helper_answer}'\n", encoding="utf-8")
    helper.chmod(0o755)
    config = tmp_path / "system.gitconfig"
    config.write_text(f'[credential "{challenge_url}"]\n\thelper = {helper}\n', encoding="utf-8")
    _git(work, "remote", "set-url", "origin", challenge_url)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for key in (*FORGE_TOKEN_ENV_KEYS, "GIT_ASKPASS", "SSH_ASKPASS"):
        monkeypatch.delenv(key, raising=False)
    spec = PushSpec(str(work), "origin", ("main",), force=False, destructive=False, unpinnable="")

    assert remote_branch_oid(str(work), "origin", "main").credential_refused
    plain = _branch_refusal(spec, "main", _ForgeRun())
    with patch.dict(os.environ, {CODEX_ROUTER_ENV: "1"}):
        routed = _branch_refusal(spec, "main", _ForgeRun())

    assert plain is not None
    assert "Ownership could not be established" in plain
    assert "Venue fault" not in plain
    assert routed is not None
    assert "Ownership could not be established" in routed
    assert "Venue fault" not in routed


def test_an_unreachable_remote_is_not_a_credential_refusal(git_scene: tuple[Path, Path, str], tmp_path: Path) -> None:
    work, _, _ = git_scene
    _git(work, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    probe = remote_branch_oid(str(work), "origin", "main")

    assert not probe.ok
    assert not probe.credential_refused


def test_slash_named_remote_resolves_its_configured_url(git_scene: tuple[Path, Path, str]) -> None:
    work, remote, oid = git_scene
    _git(work, "remote", "add", "up/stream", str(remote))

    probe = remote_branch_oid(str(work), "up/stream", "main")

    assert probe.ok
    assert probe.out == oid


def test_venue_fault_requires_a_github_remote(
    git_scene: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _, _ = git_scene
    failed = GitProbe(ok=False, out="", code=128, err="HTTP 401", credential_refused=True)
    spec = PushSpec(str(work), "origin", ("main",), force=False, destructive=False, unpinnable="")
    monkeypatch.setenv(CODEX_ROUTER_ENV, "1")
    for key in FORGE_TOKEN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    _git(work, "remote", "set-url", "origin", "https://gitlab.com/example/repo.git")

    assert _venue_fault(spec, "main", failed) is None

    _git(work, "remote", "set-url", "origin", "https://github.com/example/repo.git")
    without_token = _venue_fault(spec, "main", failed)
    assert without_token is not None
    assert "this process holds no GH_TOKEN" in without_token

    monkeypatch.setenv("GH_TOKEN", "sentinel-task-token")
    with_token = _venue_fault(spec, "main", failed)
    assert with_token is not None
    assert "GitHub rejected the GH_TOKEN" in with_token
    assert "sentinel-task-token" not in with_token


def test_active_task_token_is_scrubbed_from_probe_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "sentinel-task-token")

    result = git_probes.bounded("probe says sentinel-task-token was rejected")

    assert "sentinel-task-token" not in result
    assert "<redacted>" in result


def test_shallow_checkout_reads_live_branch_author(tmp_path: Path, git_scene: tuple[Path, Path, str]) -> None:
    work, remote, _ = git_scene
    _git(work, "checkout", "-qb", "feature")
    (work / "feature").write_text("feature", encoding="utf-8")
    _git(work, "add", "feature")
    _git(work, "-c", "user.name=Feature", "-c", "user.email=feature@example.test", "commit", "-qm", "feature")
    (work / "feature").write_text("feature again", encoding="utf-8")
    _git(work, "add", "feature")
    _git(work, "-c", "user.name=Feature", "-c", "user.email=feature@example.test", "commit", "-qm", "feature again")
    _git(work, "push", "-q", "origin", "feature")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth=1", "--no-single-branch", remote.as_uri(), str(shallow))
    _git(shallow, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    branch_oid = remote_branch_oid(str(shallow), "origin", "feature")
    assert branch_oid.ok

    live = live_remote_range(str(shallow), "origin", "feature", branch_oid.out)

    assert not live.failed
    assert "feature@example.test" in live.authors


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
    assert config_argv[-3:] == ["remote", "get-url", "origin"]
    assert ls_argv[-5:] == ["ls-remote", "--heads", "--", "file:///remote.git", "refs/heads/main"]
    assert ls_argv[2] != str(tmp_path)
    assert "-c" not in ls_argv


def test_option_shaped_remote_url_is_refused_without_running_program(
    git_scene: tuple[Path, Path, str], tmp_path: Path
) -> None:
    work, _, _ = git_scene
    marker = tmp_path / "uploadpack-ran"
    program = tmp_path / "upload:pack"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec git-upload-pack \"$@\"\n", encoding="utf-8")
    program.chmod(0o755)
    _git(work, "config", "remote.origin.url", f"--upload-pack={program}")

    with patch.object(git_probes, "git_probe", wraps=git_probes.git_probe) as probe_git:
        probe = remote_branch_oid(str(work), "origin", "main")

    assert not marker.exists()
    assert not probe.ok
    assert "starts with a dash" in probe.err
    assert all("ls-remote" not in call.args for call in probe_git.call_args_list)


def test_an_empty_remote_is_refused_rather_than_read_as_the_checkout(git_scene: tuple[Path, Path, str]) -> None:
    work, _, _ = git_scene

    probe = remote_branch_oid(str(work), "", "main")

    assert not probe.ok
    assert "is empty" in probe.err


def test_fetch_probe_runs_outside_checkout(git_scene: tuple[Path, Path, str], tmp_path: Path) -> None:
    work, remote, oid = git_scene
    marker = tmp_path / "uploadpack-ran"
    program = tmp_path / "uploadpack"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec git-upload-pack \"$@\"\n", encoding="utf-8")
    program.chmod(0o755)
    _git(work, "config", "remote.origin.uploadpack", str(program))
    _git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    with patch.object(git_probes, "git_probe", wraps=git_probes.git_probe) as probe_git:
        live = live_remote_range(str(work), "origin", "main", oid)

    assert not live.failed
    assert not marker.exists()
    fetch_argv = next(call.args for call in probe_git.call_args_list if "fetch" in call.args)
    assert fetch_argv[0] != str(work)
    assert fetch_argv[-3:] == ("--", str(remote), "refs/heads/main")


def test_fetch_probe_asks_the_remote_for_nothing_the_checkout_holds(
    git_scene: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _, oid = git_scene
    marker = tmp_path / "pack-objects-ran"
    hook = tmp_path / "pack-objects-hook"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec \"$@\"\n", encoding="utf-8")
    hook.chmod(0o755)
    config = tmp_path / "system.gitconfig"
    config.write_text(f"[uploadpack]\n\tpackObjectsHook = {hook}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    live = live_remote_range(str(work), "origin", "main", oid)

    assert not live.failed
    assert not marker.exists()


def test_first_configured_remote_url_is_probed(git_scene: tuple[Path, Path, str], tmp_path: Path) -> None:
    work, _, oid = git_scene
    empty = tmp_path / "empty.git"
    _git(tmp_path, "init", "-q", "--bare", str(empty))
    _git(work, "config", "--add", "remote.origin.url", str(empty))

    assert remote_branch_oid(str(work), "origin", "main").out == oid


def test_relative_remote_url_is_resolved_from_repo_root(git_scene: tuple[Path, Path, str]) -> None:
    work, _, oid = git_scene
    subdir = work / "subdir"
    subdir.mkdir()
    _git(work, "remote", "set-url", "origin", "../remote.git")

    assert remote_branch_oid(str(subdir), "origin", "main").out == oid


def test_bare_relative_path_is_resolved_before_neutral_probe(git_scene: tuple[Path, Path, str]) -> None:
    _, remote, oid = git_scene

    assert remote_branch_oid(str(remote), ".", "main").out == oid
