"""Spawn-model chokepoint fitness function (teatree#2216).

The spawn paths must resolve their model through
``agents/model_tiering.resolve_spawn_model`` (the per-phase tier merged with
the per-skill ``[agent.skill_models]`` floors), NEVER through the lower-level
``resolve_phase_model`` directly — bypassing the merge would silently drop
every per-skill floor, the exact regression this gate forecloses.

This AST gate walks the two spawn-path modules and turns RED if either names
``resolve_phase_model`` (import or call). The companion back-compat test below
proves that with no ``[agent.skill_models]`` / session config, the merged
``resolve_spawn_model`` returns byte-for-byte what ``resolve_phase_model``
returned — so the chokepoint changes the wiring, never the no-config behaviour.
"""

# test-path: cross-cutting
import ast
from pathlib import Path

import pytest

from teatree.agents.model_tiering import resolve_phase_model, resolve_spawn_model

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "teatree"

# The spawn-path modules that must route through resolve_spawn_model only.
_SPAWN_PATH_MODULES = (
    _SRC_ROOT / "agents" / "headless.py",
    _SRC_ROOT / "core" / "management" / "commands" / "loop_dispatch.py",
)

_FORBIDDEN_SYMBOL = "resolve_phase_model"


def _names_forbidden_symbol(path: Path) -> list[int]:
    """Lines in *path* that import or reference ``resolve_phase_model``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == _FORBIDDEN_SYMBOL for alias in node.names):
                hits.add(node.lineno)
        elif (isinstance(node, ast.Name) and node.id == _FORBIDDEN_SYMBOL) or (
            isinstance(node, ast.Attribute) and node.attr == _FORBIDDEN_SYMBOL
        ):
            hits.add(node.lineno)
    return sorted(hits)


class TestSpawnModelChokepoint:
    def test_spawn_path_modules_exist(self) -> None:
        for module in _SPAWN_PATH_MODULES:
            assert module.is_file(), module

    def test_no_spawn_path_imports_resolve_phase_model_directly(self) -> None:
        offenders = {
            str(module.relative_to(_SRC_ROOT.parent)): lines
            for module in _SPAWN_PATH_MODULES
            if (lines := _names_forbidden_symbol(module))
        }
        assert not offenders, (
            "A spawn path names resolve_phase_model directly instead of routing through "
            "resolve_spawn_model — the per-skill [agent.skill_models] floor merge would be "
            f"silently dropped (teatree#2216): {offenders}"
        )

    def test_predicate_catches_an_import(self, tmp_path: Path) -> None:
        # Anti-vacuous: the predicate actually fires on the forbidden symbol.
        bait = tmp_path / "bait.py"
        bait.write_text(
            "from teatree.agents.model_tiering import resolve_phase_model\nx = resolve_phase_model('coding')\n",
            encoding="utf-8",
        )
        assert _names_forbidden_symbol(bait)

    def test_predicate_ignores_resolve_spawn_model(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean.py"
        clean.write_text(
            "from teatree.agents.model_tiering import resolve_spawn_model\n"
            "x = resolve_spawn_model('coding', skills=[])\n",
            encoding="utf-8",
        )
        assert not _names_forbidden_symbol(clean)


class TestSpawnModelBackCompat:
    """Absent ``[agent]`` config ⇒ ``resolve_spawn_model`` == ``resolve_phase_model``.

    This is the byte-for-byte preservation guarantee: installing this feature
    with no config changes nothing about the model a spawn resolves to.
    """

    _ABSENT = Path("/nonexistent-teatree-config.toml")
    _PHASES = (
        "planning",
        "reviewing",
        "requesting_review",
        "testing",
        "shipping",
        "retrospecting",
        "coding",
        "debugging",
        "scoping",
        "unmapped-phase",
    )

    @pytest.mark.parametrize("phase", _PHASES)
    def test_matches_phase_model_with_no_skills(self, phase: str) -> None:
        assert resolve_spawn_model(phase, skills=[], config_path=self._ABSENT) == resolve_phase_model(
            phase, config_path=self._ABSENT
        )

    @pytest.mark.parametrize("phase", _PHASES)
    def test_matches_phase_model_even_with_skills_when_no_floors(self, phase: str) -> None:
        # With no skill_models table, the loaded skills contribute no floor, so
        # the result is still exactly the phase model — back-compat holds even
        # when a bundle is present.
        assert resolve_spawn_model(
            phase, skills=["code-review", "architecture-design"], config_path=self._ABSENT
        ) == resolve_phase_model(phase, config_path=self._ABSENT)
