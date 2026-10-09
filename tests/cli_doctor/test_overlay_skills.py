"""``DoctorService.collect_overlay_skills`` — overlay skill discovery.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

from teatree.cli.doctor import DoctorService

from ._shared import _seed_overlays, _stage_home


class TestCollectOverlaySkills:
    def test_returns_skills_from_skills_subdir(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        project = tmp_path / "my-project"
        skill = project / "skills" / "custom"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
        _seed_overlays(tmp_path, monkeypatch, {"my-overlay": {"path": str(project)}})

        results = DoctorService.collect_overlay_skills()

        assert (skill, "custom") in results

    def test_ignores_legacy_subdir_convention(self, tmp_path, monkeypatch):
        """Overlay skills require a ``skills/`` dir; loose SKILL.md siblings don't count."""
        _stage_home(tmp_path, monkeypatch)
        project = tmp_path / "my-overlay"
        project.mkdir()
        overlay_subdir = project / "my_app"
        overlay_subdir.mkdir()
        (overlay_subdir / "SKILL.md").touch()
        _seed_overlays(tmp_path, monkeypatch, {"my-overlay": {"path": str(project)}})

        results = DoctorService.collect_overlay_skills()

        assert all(name != "my_app" for _, name in results)

    def test_skips_entries_without_project_path_or_declared_root(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _seed_overlays(tmp_path, monkeypatch, {"classonly": {"class": "acme.overlay:AcmeOverlay"}})

        results = DoctorService.collect_overlay_skills()

        assert all(name != "classonly" for _, name in results)

    def test_declared_skill_root_works_without_project_path(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _seed_overlays(tmp_path, monkeypatch, {"classonly": {"class": "acme.overlay:AcmeOverlay"}})
        skill = tmp_path / "packaged" / "skills" / "overlay-guide"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Overlay guide\n", encoding="utf-8")

        class _Metadata:
            @staticmethod
            def get_skill_metadata() -> dict[str, str]:
                return {"skill_root": str(skill.parent)}

        class _Overlay:
            metadata = _Metadata()

        monkeypatch.setattr("teatree.core.overlay_loader.get_overlay", lambda _name: _Overlay())

        assert (skill, "overlay-guide") in DoctorService.collect_overlay_skills()
