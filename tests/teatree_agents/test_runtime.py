from pathlib import Path

import pytest

from teatree.agents.skill_bundle import resolve_skill_bundle


def test_resolve_skill_bundle_with_overlay_and_phase() -> None:
    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={"skill_path": "/skills/acme/SKILL.md"},
    )
    assert "/skills/acme/SKILL.md" in bundle
    assert "code" in bundle


def test_resolve_skill_bundle_ignores_unknown_phase() -> None:
    bundle = resolve_skill_bundle(
        phase="unknown-phase",
        overlay_skill_metadata={"skill_path": "code"},
    )
    assert "code" in bundle


def test_resolve_skill_bundle_uses_framework_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='tmp'\n", encoding="utf-8")

    bundle = resolve_skill_bundle(phase="debugging", overlay_skill_metadata={})

    assert "ac-python" in bundle
    assert "debug" in bundle
