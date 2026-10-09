# test-path: cross-cutting — drives deploy/entrypoint.sh's install precondition (no src mirror).
"""The init role refuses a ``$CLONE_DIR`` that is not teatree core.

``uv tool install --editable "$CLONE_DIR"`` writes the SHARED ``/opt/teatree/uv``
volume every role reads, so a ``$CLONE_DIR`` naming the wrong tree does not fail
the init container — it bricks the whole box. A fork whose deploy lost
``TEATREE_CLONE_DIR`` falls back to the mount root, which on a vendoring fork is
the FORK root: the install then publishes a ``teatree.pth`` naming a directory
holding neither ``teatree`` nor ``t3_bootstrap``, and every ``t3`` on the box
dies with ``ModuleNotFoundError: No module named 't3_bootstrap'``.

Per the Test-Writing Doctrine these run the REAL shell function, extracted
verbatim from the entrypoint, in a bash subprocess against real directories under
``tmp_path`` — the guard's logic is never reimplemented here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
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


def _make_core_tree(root: Path) -> Path:
    """A directory carrying the two modules the ``t3`` console script imports."""
    (root / "src" / "teatree").mkdir(parents=True)
    (root / "src" / "teatree" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "t3_bootstrap").mkdir(parents=True)
    (root / "src" / "t3_bootstrap" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _run_guard(tmp_path: Path, clone_dir: Path) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f'CLONE_DIR="{clone_dir}"\n'
        f"{_extract_shell_function('assert_core_source')}\n"
        "assert_core_source\n",
        encoding="utf-8",
    )
    return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False)


class TestAssertCoreSource:
    def test_accepts_a_core_source_tree(self, tmp_path: Path) -> None:
        result = _run_guard(tmp_path, _make_core_tree(tmp_path / "core"))

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_accepts_core_vendored_under_a_fork(self, tmp_path: Path) -> None:
        """The shape a fork's deploy exports — ``<mount>/vendor/teatree`` — is core."""
        vendored = _make_core_tree(tmp_path / "fork" / "vendor" / "teatree")
        (tmp_path / "fork" / "pyproject.toml").write_text('[project]\nname = "a-fork"\n', encoding="utf-8")

        assert _run_guard(tmp_path, vendored).returncode == 0

    def test_refuses_a_fork_root_that_only_vendors_core(self, tmp_path: Path) -> None:
        """The exact fault: ``TEATREE_CLONE_DIR`` lost, so the mount root is installed as core."""
        fork = tmp_path / "fork"
        _make_core_tree(fork / "vendor" / "teatree")
        (fork / "src" / "acme_overlay").mkdir(parents=True)
        (fork / "pyproject.toml").write_text('[project]\nname = "a-fork"\n', encoding="utf-8")

        result = _run_guard(tmp_path, fork)

        assert result.returncode != 0
        assert "is not a teatree core source tree" in result.stderr
        assert "vendor/teatree" in result.stderr

    def test_names_every_missing_module_in_one_message(self, tmp_path: Path) -> None:
        """An operator gets the whole diagnosis at once, not one module per boot."""
        empty = tmp_path / "empty"
        empty.mkdir()

        result = _run_guard(tmp_path, empty)

        assert result.returncode != 0
        assert "src/teatree/__init__.py" in result.stderr
        assert "src/t3_bootstrap/__init__.py" in result.stderr

    def test_refuses_a_tree_carrying_core_but_no_bootstrap(self, tmp_path: Path) -> None:
        """``t3_bootstrap`` is what the console script imports FIRST — its absence is the brick."""
        partial = tmp_path / "partial"
        (partial / "src" / "teatree").mkdir(parents=True)
        (partial / "src" / "teatree" / "__init__.py").write_text("", encoding="utf-8")

        result = _run_guard(tmp_path, partial)

        assert result.returncode != 0
        assert "src/t3_bootstrap/__init__.py" in result.stderr
        assert "src/teatree/__init__.py" not in result.stderr
