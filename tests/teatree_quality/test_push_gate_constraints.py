"""Push stage checks plan limits against the live diff before accepting it."""

import subprocess
from pathlib import Path
from shutil import which

from teatree.quality.push_gate import run_brief_constraints


def _git(repo: Path, *args: str) -> None:
    git_bin = which("git")
    if git_bin is None:
        message = "git executable unavailable"
        raise RuntimeError(message)
    subprocess.run([git_bin, *args], cwd=repo, check=True, capture_output=True)


def test_new_setting_named_by_plan_fails_push_check(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "fixture.invalid")
    source = tmp_path / "src" / "teatree" / "config" / "settings.py"
    source.parent.mkdir(parents=True)
    source.write_text("class AppSettings:\n    old: int = 1\n")
    _git(tmp_path, "add", "src/teatree/config/settings.py")
    _git(tmp_path, "commit", "-qm", "baseline")
    source.write_text("class AppSettings:\n    old: int = 1\n    new: int = 2\n")

    violations = run_brief_constraints(tmp_path, base_ref="HEAD", brief="Do not add a setting.")
    assert len(violations) == 1
    assert violations[0].kind == "no_setting"
    assert "AppSettings.new" in violations[0].detail


def test_renamed_module_compares_with_its_original_source(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "fixture.invalid")
    source = tmp_path / "src" / "teatree" / "config" / "old_settings.py"
    source.parent.mkdir(parents=True)
    source.write_text("class AppSettings:\n    old: int = 1\n")
    _git(tmp_path, "add", "src/teatree/config/old_settings.py")
    _git(tmp_path, "commit", "-qm", "baseline")
    renamed = source.with_name("settings.py")
    source.rename(renamed)
    _git(tmp_path, "add", "-A")

    assert run_brief_constraints(tmp_path, base_ref="HEAD", brief="Do not add a setting.") == ()

    renamed.write_text("class AppSettings:\n    old: int = 1\n    new: int = 2\n")
    violations = run_brief_constraints(tmp_path, base_ref="HEAD", brief="Do not add a setting.")
    assert len(violations) == 1
    assert "AppSettings.new" in violations[0].detail
