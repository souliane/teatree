# test-path: cross-cutting — drives deploy/entrypoint.sh against the Python floor (no src mirror).
"""The boot-time editable reinstall is gated on free space and verified afterwards (#4338).

`deploy/entrypoint.sh` runs `uv tool install … --reinstall`, which DELETES the working
tool venv before rebuilding it. On a full disk that left `typer` installed and `click`
absent, every `t3` invocation dead at import, and the worker crash-looping for 13 hours.
Two things were missing and both are pinned here: a free-space precondition that refuses
BEFORE anything is destroyed, and a post-install probe that treats a venv whose CLI cannot
start as an install failure.

`require_install_headroom` is driven for real — extracted verbatim and run in a bash
subprocess against the actual filesystem — with the floor moved by
`TEATREE_INSTALL_MIN_FREE_MB` rather than a stubbed `df`, so both directions are exercised.
The last test pins the bash default to the Python default: two implementations of one
invariant drift otherwise.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.utils.install_headroom import DEFAULT_INSTALL_MIN_FREE_MB

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash (present in the deploy image and CI)",
)

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"
_INSTALL_ARGV = 'uv tool install --editable "${CLONE_DIR}[slack]"'
_GUARD_CALL = "require_install_headroom"
_CLI_PROBE = "t3 --help >/dev/null 2>&1"


def _extract_shell_function(name: str) -> str:
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _run_guard(tool_dir: Path, floor: str | None) -> subprocess.CompletedProcess[str]:
    script = f"{_extract_shell_function(_GUARD_CALL)}\n{_GUARD_CALL}\necho GUARD_ADMITTED\n"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tool_dir), "UV_TOOL_DIR": str(tool_dir)}
    if floor is not None:
        env["TEATREE_INSTALL_MIN_FREE_MB"] = floor
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True, env=env, check=False)


def test_a_floor_above_the_measured_free_space_refuses_the_boot(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, floor="999999999")

    assert result.returncode == 1
    assert "GUARD_ADMITTED" not in result.stdout, "the guard returned instead of aborting the boot"
    assert "refusing the destructive editable reinstall" in result.stderr
    assert "MB free" in result.stderr
    assert "left INTACT" in result.stderr


def test_a_floor_below_the_measured_free_space_admits_the_install(tmp_path: Path) -> None:
    """Anti-vacuity: the guard must gate the install, not refuse unconditionally."""
    result = _run_guard(tmp_path, floor="0")

    assert result.returncode == 0
    assert "GUARD_ADMITTED" in result.stdout


def test_a_tool_dir_that_does_not_exist_yet_is_measured_via_its_parent(tmp_path: Path) -> None:
    """A fresh box has no uv tool dir — the walk up must find the filesystem it will live on."""
    result = _run_guard(tmp_path / "not" / "created" / "yet", floor="0")

    assert result.returncode == 0
    assert "GUARD_ADMITTED" in result.stdout


class TestTheGuardAndProbeBracketTheDestructiveInstall:
    def test_the_headroom_guard_precedes_the_reinstall(self) -> None:
        body = ENTRYPOINT.read_text(encoding="utf-8")

        assert body.index(f"        {_GUARD_CALL}\n") < body.index(_INSTALL_ARGV), (
            "a guard that runs after the reinstall protects nothing — the venv is already gone"
        )

    def test_a_cli_probe_follows_the_reinstall(self) -> None:
        body = ENTRYPOINT.read_text(encoding="utf-8")

        assert _CLI_PROBE in body
        assert body.index(_INSTALL_ARGV) < body.index(_CLI_PROBE)

    def test_the_probe_fails_the_boot_rather_than_warning(self) -> None:
        body = ENTRYPOINT.read_text(encoding="utf-8")
        after_probe = body[body.index(_CLI_PROBE) :]

        assert "exit 1" in after_probe[: after_probe.index("\n        fi")]


def test_the_bash_floor_and_the_python_floor_are_the_same_number() -> None:
    """One invariant, two venues: the boot-time install cannot read the Python default."""
    body = ENTRYPOINT.read_text(encoding="utf-8")

    assert f'floor="${{TEATREE_INSTALL_MIN_FREE_MB:-{DEFAULT_INSTALL_MIN_FREE_MB}}}"' in body
