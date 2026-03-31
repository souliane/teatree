from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.skill_loading import SkillLoadingPolicy, _git_remote_url, _git_remote_urls


def test_resolve_skill_bundle_basic(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n", encoding="utf-8")

    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={},
    )

    assert "code" in bundle


def test_agent_launch_explicit_phase_and_skills_raises() -> None:
    policy = SkillLoadingPolicy()
    with pytest.raises(ValueError, match="cannot be used together"):
        policy.select_for_agent_launch(
            cwd=Path("/tmp"),
            overlay_skill_metadata={},
            task="",
            ticket_status="",
            explicit_phase="coding",
            explicit_skills=["code"],
            overlay_active=False,
        )


def test_agent_launch_unknown_explicit_phase_raises() -> None:
    policy = SkillLoadingPolicy()
    with pytest.raises(ValueError, match="Unknown phase"):
        policy.select_for_agent_launch(
            cwd=Path("/tmp"),
            overlay_skill_metadata={},
            task="",
            ticket_status="",
            explicit_phase="nonsense",
            explicit_skills=[],
            overlay_active=False,
        )


def test_agent_launch_valid_explicit_phase(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="",
        ticket_status="",
        explicit_phase="coding",
        explicit_skills=[],
        overlay_active=False,
    )
    assert result.lifecycle_skill == "code"
    assert "code" in result.skills


def test_agent_launch_status_without_lifecycle_asks_user(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="",
        ticket_status="unknown-status",
        explicit_phase="",
        explicit_skills=[],
        overlay_active=False,
    )
    assert result.ask_user is True


def test_prompt_hook_with_supplementary_skills(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="code",
        overlay_skill_metadata={},
        loaded_skills=set(),
        supplementary_skills=["custom-skill"],
    )
    assert "custom-skill" in result.skills
    assert "code" in result.skills


def test_prompt_hook_no_intent(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="",
        overlay_skill_metadata={},
        loaded_skills=set(),
    )
    assert result.lifecycle_skill == ""


def test_lifecycle_for_task_no_match() -> None:
    assert SkillLoadingPolicy.lifecycle_for_task_text("hello world") == ""


def test_overlay_skill_no_lifecycle_returns_empty(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="",
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*"]},
        loaded_skills=set(),
    )
    assert "t3-acme" not in result.skills


def test_overlay_skill_non_list_patterns(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="code",
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": "not-a-list"},
        loaded_skills=set(),
    )
    assert "t3-acme" not in result.skills


def test_overlay_skill_empty_patterns(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="code",
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": []},
        loaded_skills=set(),
    )
    assert "t3-acme" not in result.skills


def test_detect_framework_python_from_setup_py(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-python"]


def test_detect_framework_python_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "foo"\n')
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-python"]


def test_detect_framework_django_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('dependencies = ["django>=5"]\n')
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-django"]


def test_detect_framework_pyproject_read_error(tmp_path: Path) -> None:
    pp = tmp_path / "pyproject.toml"
    pp.write_text("placeholder", encoding="utf-8")
    pp.chmod(0o000)
    try:
        assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == []
    finally:
        pp.chmod(0o644)


def test_detect_framework_nothing(tmp_path: Path) -> None:
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == []


def test_git_remote_urls_fallback_to_verbose(tmp_path: Path) -> None:
    mock_origin_fail = MagicMock(returncode=1, stdout="")
    mock_verbose_ok = MagicMock(
        returncode=0, stdout="upstream\tgit@gitlab.com:foo/bar (fetch)\nupstream\tgit@gitlab.com:foo/bar (push)\n"
    )

    with patch("teatree.skill_loading.subprocess.run", side_effect=[mock_origin_fail, mock_verbose_ok]):
        urls = _git_remote_urls(tmp_path)

    assert urls == ["git@gitlab.com:foo/bar"]


def test_git_remote_urls_all_fail(tmp_path: Path) -> None:
    mock_fail = MagicMock(returncode=1, stdout="")
    with patch("teatree.skill_loading.subprocess.run", return_value=mock_fail):
        urls = _git_remote_urls(tmp_path)
    assert urls == []


def test_git_remote_url_os_error(tmp_path: Path) -> None:
    with patch("teatree.skill_loading.subprocess.run", side_effect=OSError("no git")):
        assert _git_remote_url(tmp_path, "origin") == ""


def test_git_remote_urls_verbose_os_error(tmp_path: Path) -> None:
    mock_origin_fail = MagicMock(returncode=1, stdout="")
    with patch("teatree.skill_loading.subprocess.run", side_effect=[mock_origin_fail, OSError("no git")]):
        urls = _git_remote_urls(tmp_path)
    assert urls == []
