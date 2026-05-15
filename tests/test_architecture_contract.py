"""The declared module graph must stay internally consistent.

``tach.toml`` is the architecture contract: each ``[[modules]]`` entry
declares what that package may import. A concurrent merge can land a new
cross-package import without the matching ``depends_on`` edge — the
pre-commit ``tach`` hook then blocks *every* commit touching that package
until someone notices. This contract test fails the same way ``tach
check`` does, so the boundary regression is caught by the test suite
(CI / local ``pytest``) instead of only at commit time.
"""

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestArchitectureContract:
    def test_tach_check_module_graph_is_clean(self) -> None:
        # S607: trusted fixed argv (literal "uv run tach check", no user
        # input) — the repo-wide convention for subprocess in tests.
        result = subprocess.run(
            ["uv", "run", "tach", "check"],  # noqa: S607
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
