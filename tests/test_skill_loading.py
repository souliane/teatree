from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.skill_loading import (
    SkillLoadingPolicy,
    SkillSelectionResult,
    _dedupe,
    _git_remote_url,
    _git_remote_urls,
    _matches_any_remote,
)

# ── SkillSelectionResult ────────────────────────────────────────────


def test_skill_selection_result_defaults():
    result = SkillSelectionResult(skills=["a"])
    assert result.lifecycle_skill == ""
    assert result.ask_user is False


# ── SkillLoadingPolicy.lifecycle_for_status ─────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("not_started", "ticket"),
        ("started", "code"),
        ("coded", "test"),
        ("tested", "review"),
        ("reviewed", "ship"),
        ("shipped", "debug"),
        ("unknown_status", ""),
    ],
)
def test_lifecycle_for_status(status, expected):
    assert SkillLoadingPolicy.lifecycle_for_status(status) == expected


# ── SkillLoadingPolicy.lifecycle_for_phase ──────────────────────────


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("ticket-intake", "ticket"),
        ("coding", "code"),
        ("testing", "test"),
        ("reviewing", "review"),
        ("shipping", "ship"),
        ("debugging", "debug"),
        ("requesting_review", "review-request"),
        ("retrospecting", "retro"),
        ("nonexistent", ""),
    ],
)
def test_lifecycle_for_phase(phase, expected):
    assert SkillLoadingPolicy.lifecycle_for_phase(phase) == expected


# ── SkillLoadingPolicy.lifecycle_for_task_text ──────────────────────


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("debug the issue", "debug"),
        ("fix it now", "debug"),
        ("run the tests", "test"),
        ("commit and push", "ship"),
        ("review the code", "review"),
        ("start working on ticket", "ticket"),
        ("do a retro", "retro"),
        ("setup worktree", "workspace"),
        ("nothing matches here xyz", ""),
    ],
)
def test_lifecycle_for_task_text(task, expected):
    assert SkillLoadingPolicy.lifecycle_for_task_text(task) == expected


# ── SkillLoadingPolicy.select_for_agent_launch ──────────────────────


def _launch(tmp_path, **overrides):
    policy = SkillLoadingPolicy()
    defaults = {
        "cwd": tmp_path,
        "overlay_skill_metadata": {},
        "task": "",
        "ticket_status": "",
        "explicit_phase": "",
        "explicit_skills": [],
        "overlay_active": False,
    }
    defaults.update(overrides)
    return policy.select_for_agent_launch(**defaults)


def test_select_for_agent_launch_phase_and_skills_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="--phase and --skill cannot be used together"):
        _launch(tmp_path, explicit_phase="coding", explicit_skills=["test"])


def test_select_for_agent_launch_unknown_phase_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="Unknown phase: banana"):
        _launch(tmp_path, explicit_phase="banana")


def test_select_for_agent_launch_explicit_phase(tmp_path: Path):
    result = _launch(tmp_path, explicit_phase="coding")
    assert result.lifecycle_skill == "code"
    assert "code" in result.skills
    assert result.ask_user is False


def test_select_for_agent_launch_explicit_skills(tmp_path: Path):
    result = _launch(tmp_path, explicit_skills=["test", "debug"])
    assert result.skills == ["test", "debug"]
    assert result.lifecycle_skill == ""
    assert result.ask_user is False


def test_select_for_agent_launch_ticket_status(tmp_path: Path):
    result = _launch(tmp_path, ticket_status="coded")
    assert result.lifecycle_skill == "test"
    assert "test" in result.skills


def test_select_for_agent_launch_task_text(tmp_path: Path):
    result = _launch(tmp_path, task="commit the changes")
    assert result.lifecycle_skill == "ship"


def test_select_for_agent_launch_no_inputs_asks_user(tmp_path: Path):
    result = _launch(tmp_path)
    assert result.ask_user is True


def test_select_for_agent_launch_ask_user_when_no_lifecycle_no_explicit(tmp_path: Path):
    result = _launch(tmp_path, task="nothing matches xyz blah")
    assert result.ask_user is True
    assert result.lifecycle_skill == ""


def test_select_for_agent_launch_overlay_active(tmp_path: Path):
    result = _launch(
        tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme"},
        overlay_active=True,
        task="debug this",
    )
    assert "t3-acme" in result.skills
    assert "debug" in result.skills


# ── SkillLoadingPolicy.select_for_prompt_hook ───────────────────────


def test_select_for_prompt_hook_basic(tmp_path: Path):
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="code",
        overlay_skill_metadata={},
        loaded_skills=set(),
    )
    assert "code" in result.skills
    assert result.lifecycle_skill == "code"


def test_select_for_prompt_hook_filters_loaded(tmp_path: Path):
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="code",
        overlay_skill_metadata={},
        loaded_skills={"code"},
    )
    assert "code" not in result.skills


def test_select_for_prompt_hook_with_supplementary(tmp_path: Path):
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="code",
        overlay_skill_metadata={},
        loaded_skills=set(),
        supplementary_skills=["rules", "platforms"],
    )
    assert "rules" in result.skills
    assert "platforms" in result.skills


def test_select_for_prompt_hook_no_intent(tmp_path: Path):
    policy = SkillLoadingPolicy()
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="",
        overlay_skill_metadata={},
        loaded_skills=set(),
    )
    assert result.lifecycle_skill == ""


# ── SkillLoadingPolicy.select_for_runtime_phase ────────────────────


def test_select_for_runtime_phase_known(tmp_path: Path):
    policy = SkillLoadingPolicy()
    result = policy.select_for_runtime_phase(
        cwd=tmp_path,
        phase="testing",
        overlay_skill_metadata={},
    )
    assert result.lifecycle_skill == "test"
    assert "test" in result.skills


def test_select_for_runtime_phase_unknown(tmp_path: Path):
    policy = SkillLoadingPolicy()
    result = policy.select_for_runtime_phase(
        cwd=tmp_path,
        phase="unknown-phase",
        overlay_skill_metadata={},
    )
    assert result.lifecycle_skill == ""
    assert result.skills == []


def test_select_for_runtime_phase_with_overlay(tmp_path: Path):
    policy = SkillLoadingPolicy()
    result = policy.select_for_runtime_phase(
        cwd=tmp_path,
        phase="coding",
        overlay_skill_metadata={"skill_path": "t3-overlay"},
    )
    assert result.lifecycle_skill == "code"


# ── _overlay_skill_for_context ──────────────────────────────────────


def test_overlay_no_skill_path(tmp_path: Path):
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={},
        overlay_active=False,
        lifecycle_skill="code",
    )
    assert result == ""


def test_overlay_empty_skill_path(tmp_path: Path):
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "  "},
        overlay_active=False,
        lifecycle_skill="code",
    )
    assert result == ""


def test_overlay_active_returns_skill_path(tmp_path: Path):
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme"},
        overlay_active=True,
        lifecycle_skill="code",
    )
    assert result == "t3-acme"


def test_overlay_no_lifecycle_returns_empty(tmp_path: Path):
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*acme*"]},
        overlay_active=False,
        lifecycle_skill="",
    )
    assert result == ""


def test_overlay_remote_patterns_not_a_list(tmp_path: Path):
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": "not-a-list"},
        overlay_active=False,
        lifecycle_skill="code",
    )
    assert result == ""


def test_overlay_remote_patterns_empty_list(tmp_path: Path):
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": []},
        overlay_active=False,
        lifecycle_skill="code",
    )
    assert result == ""


def test_overlay_remote_patterns_with_non_string_entries(tmp_path: Path):
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": [123, None, ""]},
        overlay_active=False,
        lifecycle_skill="code",
    )
    assert result == ""


def test_overlay_remote_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._matches_any_remote",
        lambda _cwd, _patterns: True,
    )
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*acme*"]},
        overlay_active=False,
        lifecycle_skill="code",
    )
    assert result == "t3-acme"


def test_overlay_remote_no_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._matches_any_remote",
        lambda _cwd, _patterns: False,
    )
    result = SkillLoadingPolicy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*acme*"]},
        overlay_active=False,
        lifecycle_skill="code",
    )
    assert result == ""


# ── detect_framework_skills ─────────────────────────────────────────


def test_detect_manage_py(tmp_path: Path):
    (tmp_path / "manage.py").touch()
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-django"]


def test_detect_django_in_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["django>=4.2"]')
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-django"]


def test_detect_python_in_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mypkg'")
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-python"]


def test_detect_python_from_setup_py(tmp_path: Path):
    (tmp_path / "setup.py").touch()
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-python"]


def test_detect_python_from_requirements_txt(tmp_path: Path):
    (tmp_path / "requirements.txt").touch()
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == ["ac-python"]


def test_detect_nothing(tmp_path: Path):
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == []


def test_detect_pyproject_oserror(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").touch()
    monkeypatch.setattr(Path, "read_text", _raise_oserror)
    assert SkillLoadingPolicy.detect_framework_skills(tmp_path) == []


def test_detect_walks_parents(tmp_path: Path):
    subdir = tmp_path / "a" / "b" / "c"
    subdir.mkdir(parents=True)
    (tmp_path / "manage.py").touch()
    assert SkillLoadingPolicy.detect_framework_skills(subdir) == ["ac-django"]


_OSERROR_MSG = "permission denied"


def _raise_oserror(*_args, **_kwargs):
    raise OSError(_OSERROR_MSG)


# ── _dedupe ────────────────────────────────────────────────────────


def test_dedupe_preserves_order():
    assert _dedupe(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_dedupe_empty():
    assert _dedupe([]) == []


# ── _matches_any_remote ─────────────────────────────────────────────


def test_matches_any_remote_true(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_urls",
        lambda _cwd: ["git@github.com:acme/repo.git"],
    )
    assert _matches_any_remote(tmp_path, ["*acme*"]) is True


def test_matches_any_remote_false(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_urls",
        lambda _cwd: ["git@github.com:other/repo.git"],
    )
    assert _matches_any_remote(tmp_path, ["*acme*"]) is False


def test_matches_any_remote_no_urls(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_urls",
        lambda _cwd: [],
    )
    assert _matches_any_remote(tmp_path, ["*acme*"]) is False


# ── _git_remote_urls ────────────────────────────────────────────────


def _mock_remote_url(origin_url):
    def _fake(cwd, remote_name):
        if remote_name == "origin":
            return origin_url
        return ""

    return _fake


def test_git_remote_urls_with_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_url",
        _mock_remote_url("git@github.com:acme/repo.git"),
    )
    assert _git_remote_urls(tmp_path) == ["git@github.com:acme/repo.git"]


def test_git_remote_urls_fallback_to_remote_v(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_url",
        _mock_remote_url(""),
    )
    monkeypatch.setattr(
        "teatree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(
            args=[],
            returncode=0,
            stdout="upstream\tgit@github.com:acme/repo.git (fetch)\nupstream\tgit@github.com:acme/repo.git (push)\n",
        ),
    )
    result = _git_remote_urls(tmp_path)
    assert result == ["git@github.com:acme/repo.git"]


def test_git_remote_urls_fallback_multiple_remotes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_url",
        _mock_remote_url(""),
    )
    monkeypatch.setattr(
        "teatree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(
            args=[],
            returncode=0,
            stdout="fork\tgit@github.com:me/repo.git (fetch)\nupstream\tgit@github.com:acme/repo.git (fetch)\n",
        ),
    )
    result = _git_remote_urls(tmp_path)
    assert result == ["git@github.com:me/repo.git", "git@github.com:acme/repo.git"]


def test_git_remote_urls_fallback_oserror(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_url",
        _mock_remote_url(""),
    )
    monkeypatch.setattr(
        "teatree.skill_loading.subprocess.run",
        _raise_oserror,
    )
    assert _git_remote_urls(tmp_path) == []


def test_git_remote_urls_fallback_nonzero_returncode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading._git_remote_url",
        _mock_remote_url(""),
    )
    monkeypatch.setattr(
        "teatree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal"),
    )
    assert _git_remote_urls(tmp_path) == []


# ── _git_remote_url ─────────────────────────────────────────────────


def test_git_remote_url_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(
            args=[],
            returncode=0,
            stdout="git@github.com:acme/repo.git\n",
        ),
    )
    assert _git_remote_url(tmp_path, "origin") == "git@github.com:acme/repo.git"


def test_git_remote_url_oserror(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading.subprocess.run",
        _raise_oserror,
    )
    assert _git_remote_url(tmp_path, "origin") == ""


def test_git_remote_url_nonzero_returncode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teatree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )
    assert _git_remote_url(tmp_path, "origin") == ""
