"""No gate prints a registry term: a finding is named by ``path:line`` and ``class#index``.

Each gate runs end to end against a file holding a runtime-generated registry term. The
output of a gate lands in agent transcripts, CI logs and PR threads, so the value must
never be in it, whatever the gate found.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRIVACY_SCAN = ROOT / "scripts" / "privacy_scan.py"
OVERLAY_GATE = ROOT / "scripts" / "hooks" / "check_no_overlay_leak.py"
_PLANTED_LINE = 3
SCAN_TREE = "from teatree.cli.banned_terms import banned_terms_app; banned_terms_app()"


@pytest.fixture
def leak_term() -> str:
    return "zq" + secrets.token_hex(5)


@pytest.fixture
def overlay_term() -> str:
    return "ov" + secrets.token_hex(5)


@pytest.fixture
def registry_env(leak_term: str, overlay_term: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    registry = {"leak": ["first-leak", leak_term], "overlay": ["first-overlay", overlay_term]}
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps(registry))
    monkeypatch.delenv("T3_CONFIG_DB", raising=False)
    plain = {k: v for k, v in os.environ.items() if k not in {"FORCE_COLOR", "CLICOLOR_FORCE"}}
    return {**plain, "PYTHONPATH": str(ROOT / "src"), "NO_COLOR": "1"}


def _content(term: str) -> str:
    return f"first line\nsecond line\nthe {term} sits on line three\nlast line\n"


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    git_bin = shutil.which("git")
    assert git_bin is not None
    subprocess.run([git_bin, *args], cwd=repo, check=True, capture_output=True, env=env)


def _repo_with(tmp_path: Path, relpath: str, term: str) -> Path:
    repo = tmp_path / "repo"
    (repo / relpath).parent.mkdir(parents=True)
    (repo / relpath).write_text(_content(term), encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", relpath)
    _git(repo, "-c", "user.name=alice", "-c", "user.email=alice@example.invalid", "commit", "-m", "seed")
    return repo


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    return result.stdout + result.stderr


def test_scan_tree_names_the_term_by_class_and_index(
    tmp_path: Path, leak_term: str, registry_env: dict[str, str]
) -> None:
    repo = _repo_with(tmp_path, "src/app.py", leak_term)

    output = _run([sys.executable, "-c", SCAN_TREE, "scan-tree", "--repo-root", str(repo)], repo, registry_env)

    assert leak_term not in output
    assert f"src/app.py:{_PLANTED_LINE}: leak#1" in output


def test_overlay_leak_gate_names_the_term_by_class_and_index(
    tmp_path: Path, overlay_term: str, registry_env: dict[str, str]
) -> None:
    repo = _repo_with(tmp_path, "docs/note.md", overlay_term)

    output = _run([sys.executable, str(OVERLAY_GATE), "docs/note.md"], repo, registry_env)

    assert overlay_term not in output
    assert f"docs/note.md:{_PLANTED_LINE}: overlay#1" in output


def test_banned_terms_cli_names_the_term_by_class_and_index(
    tmp_path: Path, leak_term: str, registry_env: dict[str, str]
) -> None:
    repo = _repo_with(tmp_path, "notes.txt", leak_term)

    output = _run([sys.executable, "-m", "teatree.hooks.banned_terms_cli", "notes.txt"], repo, registry_env)

    assert leak_term not in output
    assert "BANNED TERM in notes.txt:" in output
    assert f"{_PLANTED_LINE}: leak#1" in output


def test_banned_terms_cli_diff_only_names_the_term_by_class_and_index(
    tmp_path: Path, leak_term: str, registry_env: dict[str, str]
) -> None:
    repo = _repo_with(tmp_path, "seed.txt", "clean")
    (repo / "notes.txt").write_text(_content(leak_term), encoding="utf-8")
    _git(repo, "add", "notes.txt")

    output = _run(
        [sys.executable, "-m", "teatree.hooks.banned_terms_cli", "--diff-only", "notes.txt"], repo, registry_env
    )

    assert leak_term not in output
    assert "BANNED TERM in notes.txt:" in output
    assert f"{_PLANTED_LINE}: leak#1" in output


def test_privacy_scan_names_the_term_by_class_and_index(
    tmp_path: Path, leak_term: str, registry_env: dict[str, str]
) -> None:
    repo = _repo_with(tmp_path, "notes.txt", leak_term)

    output = _run([sys.executable, str(PRIVACY_SCAN), "."], repo, registry_env)

    assert leak_term not in output
    assert f"notes.txt:{_PLANTED_LINE}: banned_term: leak#1" in output


def test_privacy_scan_json_names_the_term_by_class_and_index(
    tmp_path: Path, leak_term: str, registry_env: dict[str, str]
) -> None:
    repo = _repo_with(tmp_path, "notes.txt", leak_term)

    output = _run([sys.executable, str(PRIVACY_SCAN), "notes.txt", "--json"], repo, registry_env)

    assert leak_term not in output
    assert json.loads(output)[0] == {"line": _PLANTED_LINE, "category": "banned_term", "match": "leak#1"}
