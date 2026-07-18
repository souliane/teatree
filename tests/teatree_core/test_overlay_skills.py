"""The overlay skills-root resolver routes through the ``skill_root`` seam (#3355)."""

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.core import overlay_skills
from teatree.core.overlay_skills import overlay_skill_metadata, overlay_skills_root


class TestOverlaySkillsRoot:
    def test_prefers_declared_skill_root(self, tmp_path: Path) -> None:
        declared = tmp_path / "packaged" / "skills"
        root = overlay_skills_root({"skill_root": str(declared)}, tmp_path / "project")
        assert root == declared

    def test_falls_back_to_project_skills(self, tmp_path: Path) -> None:
        root = overlay_skills_root({}, tmp_path / "project")
        assert root == tmp_path / "project" / "skills"

    def test_returns_none_when_neither_available(self) -> None:
        assert overlay_skills_root({}, None) is None

    def test_blank_skill_root_falls_back(self, tmp_path: Path) -> None:
        root = overlay_skills_root({"skill_root": "   "}, tmp_path)
        assert root == tmp_path / "skills"


class TestOverlaySkillMetadata:
    def test_returns_declared_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Meta:
            @staticmethod
            def get_skill_metadata() -> dict[str, str]:
                return {"skill_root": "/somewhere/skills"}

        class _Overlay:
            metadata = _Meta()

        monkeypatch.setattr("teatree.core.overlay_loader.get_overlay", lambda _name: _Overlay())
        assert overlay_skill_metadata("t3-teatree") == {"skill_root": "/somewhere/skills"}

    def test_degrades_to_empty_when_overlay_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The build-time case: Django is not configured, so ``get_overlay`` raises
        # ``ImproperlyConfigured``; the resolver must fall back, never crash.
        def _raise(_name: str | None) -> object:
            msg = "settings are not configured"
            raise ImproperlyConfigured(msg)

        monkeypatch.setattr("teatree.core.overlay_loader.get_overlay", _raise)
        assert overlay_skill_metadata("anything") == {}

    def test_module_exports_resolver(self) -> None:
        assert set(overlay_skills.__all__) == {"overlay_skill_metadata", "overlay_skills_root"}
