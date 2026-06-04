"""Conformance ledger for the scoped (narrow) mutation-testing layer.

Sibling of ``test_chokepoints.py`` / ``test_catalog.py``. Mutation testing is
deliberately NARROW: the registry lists only verified high-value safety modules
(the OpenClaw enrichment re-weighted broad mutation DOWN). The assertions here
keep that contract honest — the registry is non-empty and every declared path
resolves to a real file on the tree (a dangling entry can never silently disarm
a module); ``scope_modules`` is the diff-vs-main ∩ registry intersection that
no-ops when no safety module is touched (so most PRs pay nothing) and selects
only the touched safety modules otherwise; and the registry stays narrow (a
guard against the "add the whole codebase" drift the design warns about).
"""

from pathlib import Path

import pytest

from teatree.quality.mutation import (
    MutationConfigError,
    load_high_value_modules,
    registry_pyproject_path,
    scope_modules,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# The design's whole point is NARROW scope. A registry that grew past this many
# entries is the "mutation everywhere" anti-pattern the OpenClaw finding warns
# against — re-justify (and bump this) before adding more.
_MAX_NARROW_REGISTRY = 12


@pytest.fixture(scope="module")
def modules() -> tuple[str, ...]:
    return load_high_value_modules(_PYPROJECT)


class TestRegistrySchema:
    def test_registry_is_non_empty(self, modules: tuple[str, ...]) -> None:
        assert modules

    def test_registry_stays_narrow(self, modules: tuple[str, ...]) -> None:
        assert len(modules) <= _MAX_NARROW_REGISTRY, (
            f"mutation registry has {len(modules)} entries — narrow by design; "
            "re-justify in the PR and bump _MAX_NARROW_REGISTRY before growing it"
        )

    def test_entries_are_unique(self, modules: tuple[str, ...]) -> None:
        assert len(modules) == len(set(modules))

    def test_entries_are_repo_relative_src_paths(self, modules: tuple[str, ...]) -> None:
        for module in modules:
            assert module.startswith("src/teatree/"), module
            assert module.endswith(".py"), module
            assert not Path(module).is_absolute(), module


class TestReachabilityLedger:
    def test_every_module_resolves_to_a_real_file(self, modules: tuple[str, ...]) -> None:
        for module in modules:
            assert (_REPO_ROOT / module).is_file(), f"declared high-value module {module!r} resolves to no file on tree"


class TestScopeModules:
    def test_no_op_when_diff_touches_no_safety_module(self, modules: tuple[str, ...]) -> None:
        changed = ("src/teatree/cli/info.py", "docs/whatever.md", "README.md")
        assert scope_modules(changed, registry=modules) == ()

    def test_selects_only_the_touched_safety_modules(self, modules: tuple[str, ...]) -> None:
        first = modules[0]
        changed = ("src/teatree/cli/info.py", first, "README.md")
        assert scope_modules(changed, registry=modules) == (first,)

    def test_selects_multiple_when_several_touched(self, modules: tuple[str, ...]) -> None:
        if len(modules) < 2:
            pytest.skip("registry has a single entry")
        changed = (modules[1], "src/teatree/cli/info.py", modules[0])
        scoped = scope_modules(changed, registry=modules)
        assert set(scoped) == {modules[0], modules[1]}

    def test_preserves_registry_order(self, modules: tuple[str, ...]) -> None:
        if len(modules) < 2:
            pytest.skip("registry has a single entry")
        changed = (modules[1], modules[0])
        assert scope_modules(changed, registry=modules) == (modules[0], modules[1])

    def test_empty_diff_is_a_no_op(self, modules: tuple[str, ...]) -> None:
        assert scope_modules((), registry=modules) == ()


class TestRegistryPath:
    def test_registry_pyproject_path_points_at_the_project_file(self) -> None:
        assert registry_pyproject_path().name == "pyproject.toml"


class TestLoaderValidation:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "pyproject.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_missing_section_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "[tool.other]\nx = 1\n")
        with pytest.raises(MutationConfigError, match="section is absent"):
            load_high_value_modules(path)

    def test_empty_list_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "[tool.teatree.mutation]\nhigh_value_modules = []\n")
        with pytest.raises(MutationConfigError, match="non-empty"):
            load_high_value_modules(path)

    def test_non_list_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, '[tool.teatree.mutation]\nhigh_value_modules = "x"\n')
        with pytest.raises(MutationConfigError, match="list of strings"):
            load_high_value_modules(path)

    def test_non_string_element_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "[tool.teatree.mutation]\nhigh_value_modules = [3]\n")
        with pytest.raises(MutationConfigError, match="list of strings"):
            load_high_value_modules(path)

    def test_real_pyproject_loads(self) -> None:
        assert load_high_value_modules(_PYPROJECT)
