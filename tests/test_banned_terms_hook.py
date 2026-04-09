import os
import subprocess
from pathlib import Path

import pytest

from teatree import find_project_root


@pytest.mark.integration
def test_banned_terms_hook_expands_tilde_config_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / ".teatree.toml"
    config.write_text('[teatree]\nbanned_terms = ["acme"]\n', encoding="utf-8")

    sample = tmp_path / "README.md"
    sample.write_text("acme overlay\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        [str(script), "--config", "~/.teatree.toml", str(sample)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert "BANNED TERM" in result.stdout


@pytest.mark.integration
def test_banned_terms_hook_ignores_matches_inside_email_addresses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / ".teatree.toml"
    config.write_text('[teatree]\nbanned_terms = ["internalterm"]\n', encoding="utf-8")

    sample = tmp_path / "AGENTS.md"
    sample.write_text("Git author: adrien <adrien.cossa@internalterm.example>\n", encoding="utf-8")

    root = find_project_root()
    assert root is not None
    script = root / "scripts" / "hooks" / "check-banned-terms.sh"
    env = dict(os.environ)
    env["HOME"] = str(home)

    result = subprocess.run(
        [str(script), "--config", "~/.teatree.toml", str(sample)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0
