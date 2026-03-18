"""Tests for smoke_test.py."""

import subprocess
from pathlib import Path

import pytest
from smoke_test import _check, _check_python_imports, main


class TestCheck:
    def test_passes_on_success(self) -> None:
        assert _check(["true"]) is True

    def test_fails_on_error(self) -> None:
        assert _check(["false"]) is False

    def test_fails_on_missing_command(self) -> None:
        assert _check(["nonexistent_command_12345_xyz"]) is False


class TestCheckPythonImports:
    def test_passes_with_valid_repo(self, tmp_path: Path) -> None:
        scripts = tmp_path / "scripts" / "lib"
        scripts.mkdir(parents=True)
        (scripts / "__init__.py").write_text("")
        (scripts / "registry.py").write_text("")
        (scripts / "env.py").write_text("")
        (scripts / "git.py").write_text("")
        assert _check_python_imports(str(tmp_path)) is True

    @pytest.mark.xfail(strict=False, reason="sys.path leak: scripts/lib already on PYTHONPATH from test runner")
    def test_fails_with_missing_modules(self, tmp_path: Path) -> None:
        assert _check_python_imports(str(tmp_path)) is False


class TestMain:
    def test_exits_1_without_t3_repo(self) -> None:
        with pytest.raises(SystemExit, match="1"):
            main(t3_repo="")

    def test_all_pass_with_valid_structure(self, tmp_path: Path) -> None:
        hook = tmp_path / "integrations" / "claude-code-statusline"
        hook.mkdir(parents=True)
        (hook / "ensure-skills-loaded.sh").write_text("#!/bin/bash\ntrue\n")
        sl = hook / "statusline-command.sh"
        sl.write_text("#!/bin/bash\ntrue\n")
        sl.chmod(0o755)
        scripts = tmp_path / "scripts" / "lib"
        scripts.mkdir(parents=True)
        (scripts / "__init__.py").write_text("")
        (scripts / "registry.py").write_text("")
        (scripts / "env.py").write_text("")
        (scripts / "git.py").write_text("")
        (tmp_path / "scripts" / "t3_cli.py").write_text("#!/usr/bin/env python3\nprint('ok')\n")

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603
            ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],  # noqa: S607
            capture_output=True,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "t@t.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "t@t.com",
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
        )
        main(t3_repo=str(tmp_path))

    def test_reports_failures(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="1"):
            main(t3_repo=str(tmp_path))
