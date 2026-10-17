"""Git probes ignore repository-local program settings."""

import shutil
import subprocess
from pathlib import Path

from hooks.scripts.foreign_branch_push_git import git_probe


def test_git_probe_ignores_repository_local_program_settings(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True)
    for key, value in (
        ("credential.helper", "fixture-helper"),
        ("core.sshCommand", "fixture-ssh"),
        ("protocol.ext.allow", "always"),
    ):
        subprocess.run([git, "-C", str(tmp_path), "config", "--local", key, value], check=True)

    assert git_probe(str(tmp_path), "config", "--get", "credential.helper").out == ""
    assert git_probe(str(tmp_path), "config", "--get", "core.sshCommand").out == ""
    assert git_probe(str(tmp_path), "config", "--get", "protocol.ext.allow").out == "never"
