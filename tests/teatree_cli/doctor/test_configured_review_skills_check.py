"""``t3 doctor`` configured-review-skill resolution check (#3352).

Closes the ``ac-reviewing-skills`` → ``ac-reviewing-codebase`` incident class at the
live config site: a ``review_skill`` / ``architectural_review_skill`` set to a name no
skill is installed for was invisible to ``t3 doctor`` — ``_check_skills`` validates only
skills already present under ``~/.claude/skills`` and is silent when that directory is
absent, so the review cadence and the reviewing-phase evidence gate dispatched an
unloadable skill with zero signal.
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from teatree.cli.doctor.checks_environment import _check_configured_review_skills, _configured_review_skill_gaps
from teatree.config.settings import UserSettings


def _seed_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")


@pytest.fixture
def canonical_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A staged skill search dir wired as the sole canonical source (the test seam)."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills))
    return skills


def _pin(monkeypatch: pytest.MonkeyPatch, settings: UserSettings, overlays: list[object] | None = None) -> None:
    monkeypatch.setattr("teatree.config.get_effective_settings", lambda *_a, **_k: settings)
    monkeypatch.setattr("teatree.config.discover_overlays", lambda: overlays or [])


class TestConfiguredReviewSkillGaps:
    def test_dangling_architectural_review_skill_flagged(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_skill(canonical_dir, "code")
        _pin(monkeypatch, replace(UserSettings(), architectural_review_skill="ac-reviewing-codebase", review_skill=""))
        gaps = _configured_review_skill_gaps()
        assert len(gaps) == 1
        assert "architectural_review_skill" in gaps[0]
        assert "ac-reviewing-codebase" in gaps[0]

    def test_installed_skill_resolves_clean(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_skill(canonical_dir, "ac-reviewing-codebase")
        _pin(monkeypatch, replace(UserSettings(), architectural_review_skill="ac-reviewing-codebase", review_skill=""))
        assert _configured_review_skill_gaps() == []

    def test_empty_review_skill_is_a_noop(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # No skill seeded at all; the empty review_skill (opt-in unset) is skipped
        # and the architectural cadence is disabled — so nothing is checked.
        _pin(
            monkeypatch,
            replace(UserSettings(), review_skill="", architectural_review_disabled=True),
        )
        assert _configured_review_skill_gaps() == []

    def test_disabled_architectural_review_skips_its_skill(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_skill(canonical_dir, "code")
        _pin(
            monkeypatch,
            replace(
                UserSettings(),
                architectural_review_disabled=True,
                architectural_review_skill="ac-reviewing-codebase",
                review_skill="",
            ),
        )
        assert _configured_review_skill_gaps() == []

    def test_opted_in_review_skill_dangling_flagged(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_skill(canonical_dir, "code")
        _pin(
            monkeypatch,
            replace(UserSettings(), review_skill="ac-reviewing-codebase", architectural_review_disabled=True),
        )
        gaps = _configured_review_skill_gaps()
        assert len(gaps) == 1
        assert "review_skill" in gaps[0]

    def test_namespaced_configured_name_resolves(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_skill(canonical_dir, "ac-reviewing-codebase")
        _pin(
            monkeypatch,
            replace(UserSettings(), review_skill="t3:ac-reviewing-codebase", architectural_review_disabled=True),
        )
        assert _configured_review_skill_gaps() == []

    def test_each_registered_overlay_is_resolved(self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_skill(canonical_dir, "code")
        _pin(
            monkeypatch,
            replace(UserSettings(), architectural_review_skill="ac-reviewing-codebase", review_skill=""),
            overlays=[SimpleNamespace(name="t3-teatree"), SimpleNamespace(name="companion")],
        )
        gaps = _configured_review_skill_gaps()
        assert len(gaps) == 2
        assert any("t3-teatree" in gap for gap in gaps)
        assert any("companion" in gap for gap in gaps)


class TestCheckConfiguredReviewSkills:
    def test_headless_worker_no_skills_installed_fails_loud(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The exact #3352 environment: no skill installed anywhere (canonical_dir
        # empty), so the default architectural_review_skill resolves to nothing.
        _pin(monkeypatch, UserSettings())
        assert _check_configured_review_skills() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "ac-reviewing-codebase" in out

    def test_installed_skill_passes(
        self, canonical_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_skill(canonical_dir, "ac-reviewing-codebase")
        _pin(monkeypatch, UserSettings())
        assert _check_configured_review_skills() is True
        assert "FAIL" not in capsys.readouterr().out

    def test_resolution_crash_degrades_to_warn(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(*_a: object, **_k: object) -> UserSettings:
            msg = "db unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr("teatree.config.get_effective_settings", _boom)
        monkeypatch.setattr("teatree.config.discover_overlays", list)
        assert _check_configured_review_skills() is True
        assert "WARN" in capsys.readouterr().out
