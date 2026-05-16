"""The declared module graph must stay internally consistent.

``tach.toml`` is the architecture contract: each ``[[modules]]`` entry
declares what that package may import. A concurrent merge can land a new
cross-package import without the matching ``depends_on`` edge — the
pre-commit ``tach`` hook then blocks *every* commit touching that package
until someone notices. This contract test fails the same way ``tach
check`` does, so the boundary regression is caught by the test suite
(CI / local ``pytest``) instead of only at commit time.

Invoked as ``{sys.executable} -m tach check`` rather than ``uv run
tach``: ``sys.executable`` is always an executable interpreter path in
every environment (local venv, the Docker test matrix), whereas a bare
``uv``/``tach`` on ``PATH`` is not guaranteed and is non-executable in
the Docker matrix.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestArchitectureContract:
    def test_tach_check_module_graph_is_clean(self) -> None:
        # S603: trusted fixed argv — sys.executable plus literal flags, no
        # user input. sys.executable is portable across every test env.
        result = subprocess.run(
            [sys.executable, "-m", "tach", "check"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(
                "tach check found a module-boundary violation — a package "
                "imports something not in its tach.toml `depends_on`:\n"
                f"{result.stdout}\n{result.stderr}",
            )
