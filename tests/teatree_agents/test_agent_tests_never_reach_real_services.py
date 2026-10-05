# test-path: cross-cutting
# Pins the tests/teatree_agents conftest guards; the code they protect is spread over several agent modules.
"""Tests under tests/teatree_agents cannot run the real `pass`, spawn the real `codex`, or read the real Codex home."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from teatree.agents.codex_auth_cache import (
    CODEX_AUTH_PASS_ENTRY,
    CodexAuthCache,
    CodexAuthCacheError,
    resolve_codex_home,
)
from teatree.utils.secrets import SecretNotFoundError, read_pass_required, write_pass


@pytest.fixture
def fake_pass_on_the_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    marker = tmp_path / "pass-was-run"
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "pass"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\necho not-a-real-secret\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    return marker


def test_a_pass_binary_on_the_path_is_never_run_by_a_read(fake_pass_on_the_path: Path) -> None:
    with pytest.raises(SecretNotFoundError):
        read_pass_required(CODEX_AUTH_PASS_ENTRY)

    assert not fake_pass_on_the_path.exists()


def test_a_pass_binary_on_the_path_is_never_run_by_a_write(fake_pass_on_the_path: Path) -> None:
    assert write_pass("teatree/codex/auth-json-b64", "value") is False
    assert not fake_pass_on_the_path.exists()


def test_the_codex_auth_cache_default_reader_cannot_reach_the_store(
    fake_pass_on_the_path: Path, tmp_path: Path
) -> None:
    with pytest.raises(CodexAuthCacheError):
        CodexAuthCache(tmp_path / "home").hydrate()

    assert not fake_pass_on_the_path.exists()


def test_the_default_codex_home_is_a_temporary_directory(tmp_path: Path) -> None:
    assert resolve_codex_home().is_relative_to(tmp_path)


def test_the_codex_binary_is_never_spawned(tmp_path: Path) -> None:
    marker = tmp_path / "codex-was-run"
    fake = tmp_path / "codex"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)

    with pytest.raises(FileNotFoundError):
        asyncio.run(asyncio.create_subprocess_exec(str(fake), "app-server"))

    assert not marker.exists()


def test_the_codex_guard_lets_every_other_program_run() -> None:
    async def run() -> int:
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        return await process.wait()

    assert asyncio.run(run()) == 0
