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

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TACH_TOML = _REPO_ROOT / "tach.toml"
#: Fenced by visibility, which layered mode would bypass (any downward import needs no edge).
_FENCED_UNLAYERED = frozenset({"teatree.slack_catchup"})


def _run_tach_check(cwd: Path) -> subprocess.CompletedProcess[str]:
    # S603: trusted fixed argv — sys.executable plus literal flags, no user
    # input. sys.executable is portable across every test env.
    return subprocess.run(
        [sys.executable, "-m", "tach", "check"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


class TestArchitectureContract:
    def test_tach_check_module_graph_is_clean(self) -> None:
        result = _run_tach_check(_REPO_ROOT)
        if result.returncode != 0:
            pytest.fail(
                "tach check found a module-boundary violation — a package "
                "imports something not in its tach.toml `depends_on`:\n"
                f"{result.stdout}\n{result.stderr}",
            )

    def test_every_module_carries_a_layer(self) -> None:
        config = tomllib.loads(_TACH_TOML.read_text(encoding="utf-8"))
        declared_layers = set(config["layers"])
        modules = {m["path"]: m for m in config["modules"]}
        unlayered = {path for path, module in modules.items() if module.get("layer") not in declared_layers}
        assert unlayered == _FENCED_UNLAYERED, f"modules missing a valid layer tag: {sorted(unlayered)}"
        rank = {layer: index for index, layer in enumerate(config["layers"])}
        for path in unlayered:
            assert modules[path].get("visibility"), f"{path} is unlayered without a visibility fence"
            for dependency in modules[path]["depends_on"]:
                assert rank[modules[dependency]["layer"]] >= rank["domain"], f"{path} reaches up to {dependency}"

    def test_layers_reject_a_backwards_cross_layer_import(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        shutil.copytree(_REPO_ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(_TACH_TOML, repo / "tach.toml")
        config = tomllib.loads((repo / "tach.toml").read_text(encoding="utf-8"))
        layer_of = {m["path"]: m.get("layer") for m in config["modules"]}
        assert layer_of["teatree.types"] == "foundation"
        assert layer_of["teatree.core"] == "domain"

        types_init = repo / "src" / "teatree" / "types.py"
        types_init.write_text(
            types_init.read_text(encoding="utf-8") + "\nfrom teatree.core.models import Ticket as _Probe\n",
            encoding="utf-8",
        )

        result = _run_tach_check(repo)
        assert result.returncode != 0, "layers config did not reject a foundation->domain import"
        assert "Layer 'foundation'" in result.stdout + result.stderr
