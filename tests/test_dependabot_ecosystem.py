"""Dependabot must watch the toolchain actually in use (souliane/teatree#4346).

The manifest ecosystem was ``pip`` while ``uv.lock`` held the resolved
versions. That ecosystem proposes bumps to ``pyproject.toml`` CONSTRAINTS, so a
security release the declared range already permits has nothing to change and
is never proposed — which is how Django 6.0.8 sat unproposed for a week under
``django>=6,<6.1``. Native ``uv`` support closed dependabot-core#10478 on
2025-04-12, "for both version updates and security updates".

That guarantee — every release the declared range admits is still proposed —
holds with version-scoped ``ignore`` entries too, and is asserted directly:
Dependabot's uv updater rewrites a pyproject bound itself (#4878, #4891), so a
hold survives only as an ignore starting exactly where the bound stops (#4900).
"""

import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"

_HELD = frozenset({canonicalize_name("click"), canonicalize_name("ruff")})

#: The ignore range that begins exactly at each ceiling operator's edge.
_IGNORE_FROM_CEILING = {"<": ">=", "<=": ">", "==": ">"}

_RUFF_HOOK_REPO = "repo: https://github.com/astral-sh/ruff-pre-commit"
#: ``yaml.safe_load`` drops comments, and the release tag lives only in the ``# vX`` one.
_RUFF_HOOK_TAG = re.compile(re.escape(_RUFF_HOOK_REPO) + r"\s*\n\s*rev: \S+\s+# v(\S+)")


def _updates() -> list[dict[str, Any]]:
    config = cast("dict[str, Any]", yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8")))
    return cast("list[dict[str, Any]]", config["updates"])


def _ecosystems() -> set[str]:
    return {str(entry["package-ecosystem"]) for entry in _updates()}


def _uv_ignores() -> list[dict[str, Any]]:
    entry = next(e for e in _updates() if e["package-ecosystem"] == "uv")
    return cast("list[dict[str, Any]]", entry.get("ignore", []))


def _declared_requirements() -> dict[NormalizedName, list[Requirement]]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    specs: list[str] = list(project.get("dependencies", []))
    for group in (*project.get("optional-dependencies", {}).values(), *data.get("dependency-groups", {}).values()):
        specs.extend(item for item in group if isinstance(item, str))
    declared: dict[NormalizedName, list[Requirement]] = {}
    for spec in specs:
        requirement = Requirement(spec)
        declared.setdefault(canonicalize_name(requirement.name), []).append(requirement)
    return declared


def _ignore_range_from_the_ceiling(requirement: Requirement) -> str | None:
    ceilings = [
        spec
        for spec in requirement.specifier
        if spec.operator in _IGNORE_FROM_CEILING and not spec.version.endswith(".*")
    ]
    if len(ceilings) != 1:
        return None
    (ceiling,) = ceilings
    return f"{_IGNORE_FROM_CEILING[ceiling.operator]}{ceiling.version}"


class TestPythonManifest:
    def test_the_lockfile_is_what_pins_resolved_versions(self) -> None:
        # The premise of the whole test: if this repo stopped being uv-locked,
        # `uv` would be the wrong ecosystem and this file should change with it.
        assert (_REPO_ROOT / "uv.lock").exists()

    def test_the_python_ecosystem_is_uv_not_pip(self) -> None:
        ecosystems = _ecosystems()
        assert "uv" in ecosystems, "a uv-locked project needs the uv ecosystem for lockfile-only security bumps"
        assert "pip" not in ecosystems, "the pip ecosystem cannot propose a bump that needs no constraint change"

    def test_the_uv_entry_watches_the_repo_root_weekly(self) -> None:
        entry = next(e for e in _updates() if e["package-ecosystem"] == "uv")
        assert entry["directory"] == "/"
        assert entry["schedule"]["interval"] == "weekly"

    def test_every_python_dependency_stays_grouped(self) -> None:
        # Behaviour preservation: the grouping the pip entry carried, unchanged.
        entry = next(e for e in _updates() if e["package-ecosystem"] == "uv")
        assert entry["groups"]["python-deps"]["patterns"] == ["*"]


class TestHoldsSurviveDependabot:
    def test_click_and_ruff_are_held_by_an_ignore(self) -> None:
        ignored = {canonicalize_name(str(ignore["dependency-name"])) for ignore in _uv_ignores()}
        assert ignored >= _HELD, (
            f"{sorted(_HELD - ignored)} carry a pyproject hold but no Dependabot ignore; the uv updater "
            "rewrites the bound itself, so the weekly group PR lifts the hold and goes red"
        )

    def test_every_ignore_is_version_scoped(self) -> None:
        unscoped = [
            ignore
            for ignore in _uv_ignores()
            if set(ignore) != {"dependency-name", "versions"} or len(ignore["versions"]) != 1
        ]
        assert not unscoped, (
            f"{unscoped}: a whole-dependency or update-types ignore also freezes the releases the "
            "declared range admits, security fixes included (#4346); name one version range"
        )

    def test_each_ignore_starts_exactly_where_the_declared_bound_stops(self) -> None:
        declared = _declared_requirements()
        mismatches: list[str] = []
        for ignore in _uv_ignores():
            name = canonicalize_name(str(ignore["dependency-name"]))
            if name not in declared:
                mismatches.append(f"{name}: ignored but not declared, so no bound says where the hold stops")
                continue
            for requirement in declared[name]:
                expected = _ignore_range_from_the_ceiling(requirement)
                if expected is None:
                    mismatches.append(f"`{requirement}`: a held dependency needs one explicit upper bound")
                elif ignore.get("versions") != [expected]:
                    mismatches.append(f"`{requirement}` stops at {expected}, the ignore says {ignore.get('versions')}")
        assert not mismatches, mismatches

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("click>=8.4,<8.5", ">=8.5"),
            ("ruff==0.15.1", ">0.15.1"),
            ("pkg<=2.0", ">2.0"),
            ("pkg>=9.0.2", None),
            ("pkg==8.4.*", None),
            ("pkg>=1,!=1.2", None),
            ("pkg<3,<=2", None),
        ],
    )
    def test_the_ignore_range_is_derived_from_the_single_ceiling(self, spec: str, expected: str | None) -> None:
        assert _ignore_range_from_the_ceiling(Requirement(spec)) == expected

    def test_the_ruff_pin_moves_in_lockstep_with_the_pre_commit_hook(self) -> None:
        config = _PRE_COMMIT.read_text(encoding="utf-8")
        hook_tags = _RUFF_HOOK_TAG.findall(config)
        assert hook_tags, f"no `{_RUFF_HOOK_REPO}` entry with a `# vX` rev tag in {_PRE_COMMIT.name}"
        assert len(hook_tags) == config.count(_RUFF_HOOK_REPO), "a ruff-pre-commit rev lost its `# vX` tag"
        (ruff,) = _declared_requirements()[canonicalize_name("ruff")]
        pinned = {spec.version for spec in ruff.specifier if spec.operator == "=="}
        assert set(hook_tags) == pinned, (
            f"pyproject pins ruff {pinned} but the CI-gating hook runs {set(hook_tags)}: local lint "
            "measures a different rule set than the gate enforces"
        )


class TestActionsManifestSurvives:
    def test_github_actions_are_still_watched(self) -> None:
        assert "github-actions" in _ecosystems()

    def test_actions_are_still_grouped_and_weekly(self) -> None:
        entry = next(e for e in _updates() if e["package-ecosystem"] == "github-actions")
        assert entry["schedule"]["interval"] == "weekly"
        assert entry["groups"]["actions"]["patterns"] == ["*"]
