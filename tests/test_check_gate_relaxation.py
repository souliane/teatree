"""Anti-relaxation prek-hook end-to-end (§17.6.1/§17.6.2, #850).

Drives the real hook ``main()`` against a real git repo under ``tmp_path`` with
a genuinely-staged diff, so the block/allow/never-lockout paths are proven on
the actual ``git diff --cached`` surface the hook reads — not a mocked diff.
"""

import subprocess
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from scripts.hooks.check_gate_relaxation import main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)  # noqa: S607 — git resolved from PATH in test


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo with one committed baseline file, cwd set into it."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "m.py")
    _git(tmp_path, "commit", "-qm", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stage(repo: Path, name: str, content: str) -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", name)


def test_staged_relaxation_blocks(repo: Path) -> None:
    _stage(repo, "m.py", "x = 1\ny = bad()  # noqa\n")
    assert main() == 1


def test_clean_staged_change_passes(repo: Path) -> None:
    _stage(repo, "m.py", "x = 1\ny = 2\n")
    assert main() == 0


def test_nothing_staged_passes(repo: Path) -> None:
    assert main() == 0


def test_allow_env_marker_lets_relaxation_through(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage(repo, "m.py", "x = 1\ny = bad()  # noqa\n")
    monkeypatch.setenv("ALLOW_GATE_RELAX", "reviewed with maintainer, vendored quirk")
    assert main() == 0


def test_empty_allow_marker_does_not_bypass(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage(repo, "m.py", "x = 1\ny = bad()  # noqa\n")
    monkeypatch.setenv("ALLOW_GATE_RELAX", "   ")
    assert main() == 1


def test_warn_only_test_vacuity_does_not_block(repo: Path) -> None:
    _stage(repo, "tests/test_x.py", "def test_it():\n    compute()\n")
    assert main() == 0


def test_scan_error_fails_open(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage(repo, "m.py", "x = 1\ny = bad()  # noqa\n")

    def _boom(_diff: str) -> list[object]:
        msg = "scan engine crashed"
        raise RuntimeError(msg)

    monkeypatch.setattr("scripts.hooks.check_gate_relaxation.scan_relaxation", _boom)
    assert main() == 0


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestDbKillSwitch:
    def test_db_kill_switch_disables_gate(self, repo: Path) -> None:
        # `t3 config_setting set gate_relaxation_gate_enabled false` is a DB write; the
        # hook must honour it via the canonical DB-first resolver. RED before the fix:
        # the old hook read the kill-switch from ~/.teatree.toml RAW, so the DB row was
        # ignored and the un-justified `# noqa` still blocked (main() stayed 1).
        _stage(repo, "m.py", "x = 1\ny = bad()  # noqa\n")
        assert main() == 1  # enabled by default -> the relaxation blocks
        call_command("config_setting", "set", "gate_relaxation_gate_enabled", "false", stdout=StringIO())
        assert main() == 0  # the DB kill-switch now actuates the hook
