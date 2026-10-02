# test-path: cross-cutting — drives deploy/entrypoint.sh's constraints block in bash; no src mirror.
"""The ambient ``UV_CONSTRAINT`` names the file the entrypoint actually writes (#4659).

``deploy/Dockerfile`` sets ``UV_CONSTRAINT="${TEATREE_CLONE_DIR}/uv-constraints.txt"``, which
Docker resolves at BUILD time against the image default clone dir. ``deploy/entrypoint.sh``
writes its constraints file at the RUNTIME ``$TEATREE_CLONE_DIR``. On a standalone clone the
two coincide; on a layout that vendors core under ``vendor/teatree`` they do not, and the
ambient value then names a path nothing ever writes. ``uv tool install`` errors outright on a
missing constraints path, so under ``set -e`` the init role exits 2 at ``uv tool install
prek`` — the one install that carries no explicit ``--constraints`` — and every role gated on
``service_completed_successfully`` stays in ``Created``.

The block is driven for real: extracted verbatim from the shipped entrypoint and run in a bash
subprocess against a nested ``vendor/teatree`` layout, with ``uv`` stubbed so both the export
branch and the comment-only fallback are exercised. The alignment is asserted BEFORE
``ensure_uv_constraints`` is called, because that function has a single call site in the
``init`` role — a role that never calls it (worker, slack-listener, admin, and every exec'd
``t3 update``) must still inherit a value that resolves.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash (present in the deploy image and CI)",
)

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"
_SLICE_START = 'CONSTRAINTS_FILE="${CLONE_DIR}/uv-constraints.txt"'
_FUNCTION = "ensure_uv_constraints"

#: The image ENV a vendored layout inherits — resolved at build time, so it names the fork
#: root rather than the runtime clone dir. The entrypoint must overwrite it, not read it.
_STALE_AMBIENT = "/home/teatree/teatree/uv-constraints.txt"


def _constraints_block() -> str:
    """The shipped ``CONSTRAINTS_FILE`` assignment through the end of ``ensure_uv_constraints``."""
    lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(_SLICE_START)), None)
    assert start is not None, f"{_SLICE_START!r} not found in {ENTRYPOINT}"
    end = next((i for i in range(start, len(lines)) if lines[i] == "}"), None)
    assert end is not None, f"unterminated {_FUNCTION} in {ENTRYPOINT}"
    return "\n".join(lines[start : end + 1])


def _stub_uv(bin_dir: Path, *, succeeds: bool) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    body = (
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { printf "pinned==1.0\\n" >"$2"; exit 0; }; shift; done\nexit 0\n'
        if succeeds
        else "exit 1\n"
    )
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    uv.chmod(0o755)


def _run(clone_dir: Path, bin_dir: Path, *, call_the_function: bool) -> subprocess.CompletedProcess[str]:
    invocation = f"{_FUNCTION}\n" if call_the_function else ""
    script = (
        f"CLONE_DIR={clone_dir}\n"
        f"{_constraints_block()}\n"
        f"{invocation}"
        'printf "UV_CONSTRAINT=%s\\n" "$UV_CONSTRAINT"\n'
        'printf "RESOLVES=%s\\n" "$([ -f "$UV_CONSTRAINT" ] && echo YES || echo NO)"\n'
    )
    env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "UV_CONSTRAINT": _STALE_AMBIENT}
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True, env=env, check=False)


@pytest.fixture
def vendored(tmp_path: Path) -> Path:
    """A fork root with core vendored under ``vendor/teatree`` — the layout the bug needs."""
    clone_dir = tmp_path / "teatree" / "vendor" / "teatree"
    clone_dir.mkdir(parents=True)
    return clone_dir


class TestTheAmbientConstraintFollowsTheRuntimeCloneDir:
    def test_the_exported_path_resolves_after_the_export_branch(self, vendored: Path, tmp_path: Path) -> None:
        _stub_uv(tmp_path / "bin", succeeds=True)
        result = _run(vendored, tmp_path / "bin", call_the_function=True)
        assert "RESOLVES=YES" in result.stdout, (
            "after `ensure_uv_constraints` returns, the file named by UV_CONSTRAINT must exist: "
            "`uv tool install prek` carries no explicit --constraints and errors outright on a "
            f"missing path, taking the init role down with it. Got: {result.stdout}{result.stderr}"
        )

    def test_the_exported_path_resolves_after_the_comment_only_fallback(self, vendored: Path, tmp_path: Path) -> None:
        # The fallback exists PRECISELY because uv errors on a missing constraints file, so it
        # is the branch that must not leave the ambient value pointing somewhere else.
        _stub_uv(tmp_path / "bin", succeeds=False)
        result = _run(vendored, tmp_path / "bin", call_the_function=True)
        assert "RESOLVES=YES" in result.stdout, (
            "the comment-only fallback must also leave UV_CONSTRAINT naming a file that exists. "
            f"Got: {result.stdout}{result.stderr}"
        )

    def test_a_role_that_never_calls_the_function_still_inherits_a_resolving_path(
        self, vendored: Path, tmp_path: Path
    ) -> None:
        # `ensure_uv_constraints` has ONE call site, in the `init` role. The worker,
        # slack-listener and admin roles — and `t3 update`'s `uv tool install --editable`
        # inside them — never run it, so the alignment cannot live inside the function.
        _stub_uv(tmp_path / "bin", succeeds=True)
        result = _run(vendored, tmp_path / "bin", call_the_function=False)
        assert f"UV_CONSTRAINT={vendored / 'uv-constraints.txt'}" in result.stdout, (
            "UV_CONSTRAINT must be aligned to the runtime clone dir at a scope every role "
            f"reaches, not inside the init-only `{_FUNCTION}`. Got: {result.stdout}{result.stderr}"
        )

    def test_the_harness_drives_the_shipped_block(self, vendored: Path, tmp_path: Path) -> None:
        # The control: a slice that lost the function would satisfy the assertions above by
        # writing nothing at all, and a stale stub would pass them without running any uv.
        block = _constraints_block()
        assert f"{_FUNCTION}() {{" in block
        assert "uv export" in block
        _stub_uv(tmp_path / "bin", succeeds=True)
        _run(vendored, tmp_path / "bin", call_the_function=True)
        assert (vendored / "uv-constraints.txt").read_text(encoding="utf-8") == "pinned==1.0\n"
