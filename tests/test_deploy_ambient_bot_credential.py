import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _deploy_wrapper_paths import container_credential_prologue

_BASH = shutil.which("bash") or "/bin/bash"
_SH = shutil.which("sh") or "/bin/sh"
_ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"


@pytest.mark.parametrize(
    "case",
    [
        ("worker", "person-key", 1, "", True),
        ("worker", "bot-key", 0, "bot-key", True),
        ("worker", "", 1, "", False),
        ("init", "person-key", 1, "person-key", False),
    ],
)
def test_runtime_checks_ambient_gitlab_identity_before_start(
    tmp_path: Path, case: tuple[str, str, int, str, bool]
) -> None:
    role, token, verify_exit, expected_token, called = case
    source = _ENTRYPOINT.read_text()
    start = source.index('if [ "$ROLE" != init ] && [ -n "${GITLAB_TOKEN:-}" ]; then')
    end = source.index('\ncase "$ROLE" in', start)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    t3 = bin_dir / "t3"
    t3.write_text("#!/bin/sh\nexit 0\n")
    t3.chmod(0o755)
    python = bin_dir / "python"
    python.write_text('#!/bin/sh\nprintf called > "$CALL_LOG"\nexit "$VERIFY_EXIT"\n')
    python.chmod(0o755)
    call_log = tmp_path / "called"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ROLE": role,
        "GITLAB_TOKEN": token,
        "CALL_LOG": str(call_log),
        "VERIFY_EXIT": str(verify_exit),
    }

    result = subprocess.run(
        [_BASH, "-c", source[start:end] + '\nprintf "token=%s" "$GITLAB_TOKEN"'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"token={expected_token}"
    assert call_log.exists() == called


@pytest.mark.parametrize(("verify_exit", "expected"), [(1, 69), (0, 0)])
def test_container_cli_checks_forwarded_gitlab_identity(tmp_path: Path, verify_exit: int, expected: int) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    t3 = bin_dir / "t3"
    t3.write_text("#!/bin/sh\nexit 0\n")
    t3.chmod(0o755)
    python = bin_dir / "python"
    python.write_text(f"#!/bin/sh\nexit {verify_exit}\n")
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITLAB_TOKEN": "person-key",
        "TEATREE_VERIFY_GITLAB_TOKEN": "1",
    }

    result = subprocess.run(
        [_SH, "-c", container_credential_prologue(), "t3-in-container", _SH, "-c", "printf reached"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected
    assert (result.stdout == "reached") == (expected == 0)
