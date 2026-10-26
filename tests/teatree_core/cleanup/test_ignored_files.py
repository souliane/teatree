"""Ignored-file classification through a real git checkout."""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.cleanup.checkout_registry import CACHEDIR_SIGNATURE
from teatree.core.cleanup.ignored_files import ignored_unregenerable
from teatree.core.runners.worktree_provision import _append_envrc_lines
from tests.teatree_core.cleanup._shared import _run_git


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=repo)
    _run_git("config", "user.email", "t@t", cwd=repo)
    _run_git("config", "user.name", "t", cwd=repo)
    (repo / ".gitignore").write_text("notes.local\n.envrc\ncache/\n__pycache__/\n", encoding="utf-8")
    _run_git("add", ".gitignore", cwd=repo)
    _run_git("commit", "-q", "-m", "base", cwd=repo)
    return repo


def test_unregenerable_ignored_file_is_named(checkout: Path) -> None:
    (checkout / "notes.local").write_text("retain", encoding="utf-8")

    assert ignored_unregenerable(checkout) == ["notes.local"]


def test_regenerable_ignored_entries_are_not_work(checkout: Path) -> None:
    (checkout / ".git" / "info" / "exclude").write_text(
        ".local/\nplugins/\nsite/\nARCHITECTURE.md\n.pr-body.md\n", encoding="utf-8"
    )
    for directory in (".local", "plugins", "site"):
        target = checkout / directory
        target.mkdir()
        (target / "artifact").write_text("rebuild\n", encoding="utf-8")
    for name in ("ARCHITECTURE.md", ".pr-body.md"):
        (checkout / name).write_text("rebuild\n", encoding="utf-8")

    assert ignored_unregenerable(checkout) == []


def test_generated_artifacts_and_tagged_cache_are_ignored(checkout: Path) -> None:
    cache = checkout / "cache"
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIGNATURE)
    (cache / "artifact").write_text("rebuild", encoding="utf-8")
    pycache = checkout / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.pyc").write_bytes(b"compiled")

    assert ignored_unregenerable(checkout) == []


def test_unreadable_ignored_subdirectory_refuses_a_complete_read(checkout: Path) -> None:
    cache = checkout / "cache"
    cache.mkdir()
    (cache / "private.txt").write_text("retain", encoding="utf-8")

    with patch("os.walk", side_effect=PermissionError("denied")), pytest.raises(PermissionError, match="denied"):
        ignored_unregenerable(checkout)


def test_envrc_is_regenerable_only_while_its_content_is_provisioned(checkout: Path) -> None:
    _append_envrc_lines(str(checkout), ["use flake", "export A=1"])
    assert ignored_unregenerable(checkout) == []

    with (checkout / ".envrc").open("a", encoding="utf-8") as envrc:
        envrc.write("export PRIVATE=1\n")

    assert ignored_unregenerable(checkout) == [".envrc"]


def test_preexisting_handwritten_envrc_is_never_recorded(checkout: Path) -> None:
    (checkout / ".envrc").write_text("export PRIVATE=1\n", encoding="utf-8")
    _append_envrc_lines(str(checkout), ["use flake"])

    assert ignored_unregenerable(checkout) == [".envrc"]


@pytest.mark.parametrize("content", [" use flake\n", "use flake  \n"])
def test_preexisting_envrc_line_must_match_provisioning_exactly(checkout: Path, content: str) -> None:
    (checkout / ".envrc").write_text(content, encoding="utf-8")
    _append_envrc_lines(str(checkout), ["use flake"])

    assert ignored_unregenerable(checkout) == [".envrc"]
