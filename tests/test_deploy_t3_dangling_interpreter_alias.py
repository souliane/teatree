"""``warn_dangling_interpreter_aliases`` — the one check only the HOST can make (#4642).

The interpreter root is bound at path identity, so an alias ``uv`` writes into it
is valid in both venues. While it was not — while the container mounted that root
at its own ``/home/teatree`` coordinate on a host whose home is something else —
every online boot's ``uv python install`` rewrote the HOST's alias set with
container-absolute targets, and nothing on the box could say so: the ``t3``
wrapper execs INTO the container, where ``t3 doctor``'s interpreter-plane check
reads the container's view and every rewritten alias resolves perfectly.

This wrapper is the only layer that executes on the host, so the check lives here.
Runs the REAL shell function, extracted verbatim from ``deploy/t3``, in a bash
subprocess against real symlinks under ``tmp_path``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present on the host and in CI)")

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_BASH = shutil.which("bash") or "bash"
_FUNCTION = "warn_dangling_interpreter_aliases"


def _extract_shell_function(name: str) -> str:
    """The verbatim source of shell function *name* from the wrapper."""
    body: list[str] = []
    capturing = False
    for line in WRAPPER.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {WRAPPER}"
    raise AssertionError(not_found)


def _run(tmp_path: Path, root: Path) -> str:
    """Stderr from the real function, run under the wrapper's own `set -euo pipefail`.

    ``check=True`` is the second assertion in every test below: this is a WARNING
    on the path of every host `t3` command, so a non-zero exit would take the CLI
    down with it.
    """
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n" + _extract_shell_function(_FUNCTION) + f'\n{_FUNCTION} "{root}"\n',
        encoding="utf-8",
    )
    return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=True).stderr


def _root_with_alias(tmp_path: Path, target: Path) -> Path:
    root = tmp_path / "uv" / "python"
    root.mkdir(parents=True, exist_ok=True)
    (root / "cpython-3.13-macos-aarch64-none").symlink_to(target)
    return root


def test_a_dangling_alias_is_named_with_the_target_it_records(tmp_path: Path) -> None:
    # The real shape: the alias points at a path that exists only in the container.
    container_only = Path("/home/teatree/.local/share/uv/python/cpython-3.13.15-linux-aarch64-gnu")
    root = _root_with_alias(tmp_path, container_only)

    message = _run(tmp_path, root)

    assert "dangling interpreter alias" in message
    assert str(root / "cpython-3.13-macos-aarch64-none") in message
    assert str(container_only) in message, "the operator needs the target to see WHICH venue wrote it"


def test_an_alias_that_resolves_here_is_silent(tmp_path: Path) -> None:
    # The no-false-positive case the fix creates: under a true identity mount the
    # target uv records is a real directory in this venue, so a healthy root must
    # print nothing on a path that runs before EVERY host `t3` command.
    real = tmp_path / "uv" / "python" / "cpython-3.13.12-macos-aarch64-none"
    real.mkdir(parents=True)
    root = _root_with_alias(tmp_path, real)

    assert _run(tmp_path, root) == ""


def test_a_root_that_does_not_exist_yet_is_silent(tmp_path: Path) -> None:
    # A host that has never run uv. The glob matches nothing and must not be
    # mistaken for a dangling entry, nor fail under `set -u`.
    assert _run(tmp_path, tmp_path / "never-created") == ""


def test_a_real_interpreter_directory_is_not_reported(tmp_path: Path) -> None:
    # Only SYMLINKS are aliases; the versioned install dirs sit in the same root.
    root = tmp_path / "uv" / "python"
    (root / "cpython-3.13.12-macos-aarch64-none" / "bin").mkdir(parents=True)

    assert _run(tmp_path, root) == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
