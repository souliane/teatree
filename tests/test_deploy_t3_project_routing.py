# test-path: cross-cutting — executes the real deploy/t3 against two stubbed Compose projects.
"""The host wrapper resolves the same project for its label probe and Compose dispatch."""

import os
import shutil
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_BASH = shutil.which("bash") or "/bin/bash"


def test_real_host_wrapper_targets_the_configured_project_with_two_stacks_present(tmp_path: Path) -> None:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    docker = stub_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >>"$DOCKER_CALLS"\n'
        'case "$1" in\n'
        'ps) case "$*" in\n'
        "  *project=zddproof*) printf 'proof-worker teatree-worker False\\n' ;;\n"
        "  *project=teatree*) printf 'primary-worker teatree-worker False\\n' ;;\n"
        "  esac ;;\n"
        "compose|version|inspect) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    calls = tmp_path / "docker.calls"
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEATREE_")}
    env.update(
        PATH=f"{stub_dir}:{env['PATH']}",
        HOME=str(tmp_path),
        DOCKER_CALLS=str(calls),
        TEATREE_COMPOSE_PROJECT="zddproof",
        COMPOSE_PROJECT_NAME="teatree",
    )

    result = subprocess.run(
        [_BASH, str(WRAPPER), "teatree", "info"], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    commands = calls.read_text(encoding="utf-8").splitlines()
    assert any("label=com.docker.compose.project=zddproof" in command for command in commands)
    assert not any("label=com.docker.compose.project=teatree " in command for command in commands)
    assert any(command.startswith("compose -p zddproof ") and " exec " in command for command in commands)
