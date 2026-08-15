"""Dependabot must watch the toolchain actually in use (souliane/teatree#4346).

The manifest ecosystem was ``pip`` while ``uv.lock`` held the resolved
versions. That ecosystem proposes bumps to ``pyproject.toml`` CONSTRAINTS, so a
security release the declared range already permits has nothing to change and
is never proposed — which is how Django 6.0.8 sat unproposed for a week under
``django>=6,<6.1``. Native ``uv`` support closed dependabot-core#10478 on
2025-04-12, "for both version updates and security updates".
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"


def _updates() -> list[dict[str, Any]]:
    config = cast("dict[str, Any]", yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8")))
    return cast("list[dict[str, Any]]", config["updates"])


def _ecosystems() -> set[str]:
    return {str(entry["package-ecosystem"]) for entry in _updates()}


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

    def test_no_dependency_is_frozen_out_of_security_updates(self) -> None:
        entry = next(e for e in _updates() if e["package-ecosystem"] == "uv")
        assert "ignore" not in entry, "an ignore entry re-opens the surfacing gap this issue is about"


class TestActionsManifestSurvives:
    def test_github_actions_are_still_watched(self) -> None:
        assert "github-actions" in _ecosystems()

    def test_actions_are_still_grouped_and_weekly(self) -> None:
        entry = next(e for e in _updates() if e["package-ecosystem"] == "github-actions")
        assert entry["schedule"]["interval"] == "weekly"
        assert entry["groups"]["actions"]["patterns"] == ["*"]
