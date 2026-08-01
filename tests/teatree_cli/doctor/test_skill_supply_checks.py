"""``t3 doctor`` skill-supply gates: dispatched-but-absent, and installed-but-stale.

Both close the same silence. An overlay's per-phase ``stage_skills`` map is a
declaration surface no existing provisioning reader sees, so a stage skill that
resolves to nothing loaded nothing while the phase carried on; and an installed
skill is a physical copy, so a merged fix could sit unreached on every box with
``t3 doctor`` reporting green.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from teatree.cli.doctor.checks_skill_supply import (
    _check_dispatched_overlay_skills,
    _check_skill_source_drift,
    _dispatched_skill_gaps,
)
from teatree.provisioning.skill_drift import SkillSourceClone
from tests._git_repo import make_git_repo, run_git


def _seed_skill(root: Path, name: str, body: str = "reviewed") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}\n", encoding="utf-8")


def _overlay(
    *,
    stage_skills: dict[str, list[str]] | None = None,
    companion_skills: list[str] | None = None,
    pr_review_companion: str = "",
    skill_source_clones: list[SkillSourceClone] | None = None,
) -> SimpleNamespace:
    config = SimpleNamespace(
        stage_skills=stage_skills or {},
        companion_skills=companion_skills or [],
        pr_review_companion=pr_review_companion,
        skill_source_clones=skill_source_clones or [],
    )
    return SimpleNamespace(config=config)


def _source(clone: Path) -> SkillSourceClone:
    return SkillSourceClone(label="team/skills", paths=[str(clone)])


def _pin_overlays(monkeypatch: pytest.MonkeyPatch, overlays: dict[str, object]) -> None:
    monkeypatch.setattr("teatree.core.overlay_loader.get_all_overlays", lambda: overlays)


@pytest.fixture
def canonical_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The staged skill search dir wired as the sole canonical source (the test seam)."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills))
    return skills


class TestDispatchedSkillGaps:
    def test_stage_skill_with_no_installed_body_is_flagged(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The wiring incident: a phase dispatches a skill the box never got, the
        # loader warns into a log, and the stage runs without it.
        _seed_skill(canonical_dir, "prd-agent")
        dispatch = {"planning": ["prd-agent", "bdd-test-creation"]}
        _pin_overlays(monkeypatch, {"t3-alpha": _overlay(stage_skills=dispatch)})
        gaps = _dispatched_skill_gaps()
        assert len(gaps) == 1
        assert "bdd-test-creation" in gaps[0]
        assert "stage_skills[planning]" in gaps[0]
        assert "t3-alpha" in gaps[0]

    def test_fully_installed_dispatch_map_is_clean(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("prd-agent", "bdd-test-creation", "teatree", "code-review"):
            _seed_skill(canonical_dir, name)
        _pin_overlays(
            monkeypatch,
            {
                "t3-alpha": _overlay(
                    stage_skills={"planning": ["prd-agent", "bdd-test-creation"], "testing": ["bdd-test-creation"]},
                    companion_skills=["teatree"],
                    pr_review_companion="code-review",
                )
            },
        )
        assert _dispatched_skill_gaps() == []

    def test_companion_and_review_companion_are_covered(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_overlays(
            monkeypatch,
            {"t3-alpha": _overlay(companion_skills=["teatree"], pr_review_companion="code-review")},
        )
        gaps = _dispatched_skill_gaps()
        assert len(gaps) == 2
        assert any("companion_skills" in gap and "teatree" in gap for gap in gaps)
        assert any("pr_review_companion" in gap and "code-review" in gap for gap in gaps)

    def test_empty_review_companion_dispatches_nothing(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_overlays(monkeypatch, {"t3-beta": _overlay(pr_review_companion="")})
        assert _dispatched_skill_gaps() == []

    def test_namespaced_dispatch_name_resolves(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_skill(canonical_dir, "bdd-test-creation")
        _pin_overlays(monkeypatch, {"t3-alpha": _overlay(stage_skills={"testing": ["t3:bdd-test-creation"]})})
        assert _dispatched_skill_gaps() == []

    def test_every_registered_overlay_is_enumerated(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin_overlays(
            monkeypatch,
            {
                "t3-alpha": _overlay(stage_skills={"planning": ["prd-agent"]}),
                "t3-beta": _overlay(companion_skills=["slack-formatting"]),
            },
        )
        gaps = _dispatched_skill_gaps()
        assert len(gaps) == 2
        assert any("t3-alpha" in gap for gap in gaps)
        assert any("t3-beta" in gap for gap in gaps)


class TestCheckDispatchedOverlaySkills:
    def test_missing_dispatched_skill_fails_loud(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _pin_overlays(monkeypatch, {"t3-alpha": _overlay(stage_skills={"planning": ["prd-agent"]})})
        assert _check_dispatched_overlay_skills() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "prd-agent" in out

    def test_clean_dispatch_map_passes_silently(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_skill(canonical_dir, "prd-agent")
        _pin_overlays(monkeypatch, {"t3-alpha": _overlay(stage_skills={"planning": ["prd-agent"]})})
        assert _check_dispatched_overlay_skills() is True
        assert "FAIL" not in capsys.readouterr().out

    def test_enumeration_crash_degrades_to_warn(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom() -> dict[str, object]:
            msg = "overlay registry unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr("teatree.core.overlay_loader.get_all_overlays", _boom)
        assert _check_dispatched_overlay_skills() is True
        assert "WARN" in capsys.readouterr().out


@pytest.fixture
def source_clone(tmp_path: Path) -> Path:
    origin = make_git_repo(tmp_path / "origin")
    _seed_skill(origin, "backend-dev")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "publish")
    run_git(tmp_path, "clone", "-q", str(origin), str(tmp_path / "clone"))
    return tmp_path / "clone"


class TestCheckSkillSourceDrift:
    def test_stale_install_fails_and_names_the_skills(
        self,
        canonical_dir: Path,
        source_clone: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_skill(canonical_dir, "backend-dev", body="the old instructions")
        _pin_overlays(
            monkeypatch,
            {"t3-alpha": _overlay(skill_source_clones=[_source(source_clone)])},
        )
        assert _check_skill_source_drift() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "backend-dev" in out
        assert "team/skills" in out

    def test_current_install_passes(
        self,
        canonical_dir: Path,
        source_clone: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_skill(canonical_dir, "backend-dev")
        _pin_overlays(
            monkeypatch,
            {"t3-alpha": _overlay(skill_source_clones=[_source(source_clone)])},
        )
        assert _check_skill_source_drift() is True
        assert "FAIL" not in capsys.readouterr().out

    def test_missing_clone_warns_without_failing_the_gate(
        self, canonical_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A deployed image has no clone: unverified must read as unverified, not
        # as a pass and not as a failure the operator cannot act on.
        _pin_overlays(
            monkeypatch,
            {
                "t3-alpha": _overlay(
                    skill_source_clones=[SkillSourceClone(label="team/skills", paths=[str(tmp_path / "nowhere")])]
                )
            },
        )
        assert _check_skill_source_drift() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "UNVERIFIED" in out

    def test_overlay_declaring_no_source_is_inert(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _pin_overlays(monkeypatch, {"t3-beta": _overlay()})
        assert _check_skill_source_drift() is True
        assert capsys.readouterr().out == ""

    def test_two_overlays_declaring_the_same_source_report_once(
        self,
        canonical_dir: Path,
        source_clone: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_skill(canonical_dir, "backend-dev", body="the old instructions")
        clone = _source(source_clone)
        _pin_overlays(
            monkeypatch,
            {
                "t3-alpha": _overlay(skill_source_clones=[clone]),
                "t3-gamma": _overlay(skill_source_clones=[clone]),
            },
        )
        assert _check_skill_source_drift() is False
        assert capsys.readouterr().out.count("FAIL") == 1

    def test_measurement_crash_degrades_to_warn(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom() -> dict[str, object]:
            msg = "overlay registry unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr("teatree.core.overlay_loader.get_all_overlays", _boom)
        assert _check_skill_source_drift() is True
        assert "WARN" in capsys.readouterr().out
