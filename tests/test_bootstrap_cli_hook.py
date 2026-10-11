# test-path: cross-cutting — runs the SessionStart shell hook under a stripped PATH; no src mirror.
"""The SessionStart hook says so when a Docker install cannot keep its MCP server across restarts."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.docker.workflow import LAUNCHER_MARKER

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "bootstrap-cli.sh"
BASH = shutil.which("bash")
WARNING = "no python3 >= 3.9"


def _bin(tmp_path: Path, *, launcher: bool, python: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("grep", "readlink"):
        found = shutil.which(tool)
        assert found is not None
        (bin_dir / tool).symlink_to(found)
    t3 = bin_dir / "t3"
    body = f'#!/usr/bin/env bash\n{LAUNCHER_MARKER}\nexec /checkout/deploy/t3 "$@"\n' if launcher else "#!/bin/sh\n"
    t3.write_text(body, encoding="utf-8")
    t3.chmod(0o755)
    if python == "current":
        (bin_dir / "python3").symlink_to(sys.executable)
    elif python == "too-old":
        old = bin_dir / "python3"
        old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        old.chmod(0o755)
    return bin_dir


def _run(bin_dir: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(HOOK)], capture_output=True, text=True, env={"PATH": str(bin_dir)}, timeout=30, check=False
    )


@pytest.mark.parametrize("python", ["absent", "too-old"])
def test_a_docker_launcher_without_a_usable_python_warns_the_session(tmp_path: Path, python: str) -> None:
    completed = _run(_bin(tmp_path, launcher=True, python=python))

    assert completed.returncode == 0
    assert WARNING in completed.stdout
    assert "/mcp" in completed.stdout


@pytest.mark.parametrize("relative", [False, True])
def test_a_t3_symlinked_to_deploy_t3_without_python_warns_the_session(tmp_path: Path, *, relative: bool) -> None:
    bin_dir = _bin(tmp_path, launcher=False, python="absent")
    wrapper = tmp_path / "checkout" / "deploy" / "t3"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (bin_dir / "t3").unlink()
    (bin_dir / "t3").symlink_to(Path("..") / "checkout" / "deploy" / "t3" if relative else wrapper)

    completed = _run(bin_dir)

    assert completed.returncode == 0
    assert WARNING in completed.stdout


def test_a_docker_launcher_with_python_stays_quiet(tmp_path: Path) -> None:
    completed = _run(_bin(tmp_path, launcher=True, python="current"))

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_a_native_t3_needs_no_proxy_and_stays_quiet(tmp_path: Path) -> None:
    completed = _run(_bin(tmp_path, launcher=False, python="absent"))

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_the_python_free_path_really_has_no_python(tmp_path: Path) -> None:
    bin_dir = _bin(tmp_path, launcher=True, python="absent")

    assert shutil.which("python3", path=str(bin_dir)) is None
    assert shutil.which("t3", path=str(bin_dir)) is not None
