from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teetree.skill_loading import (
    SkillLoadingPolicy,
    SkillSelectionResult,
    find_skill_md,
    parse_skill_requires,
    resolve_dependencies,
)

# ── parse_skill_requires ────────────────────────────────────────────


def test_parse_skill_requires_empty_string():
    assert parse_skill_requires("") == []


def test_parse_skill_requires_no_frontmatter():
    assert parse_skill_requires("# Just markdown") == []


def test_parse_skill_requires_no_closing_fence():
    assert parse_skill_requires("---\nrequires:\n  - skill-a\n") == []


def test_parse_skill_requires_no_requires_key():
    assert parse_skill_requires("---\nname: foo\n---\n") == []


def test_parse_skill_requires_single_dep():
    md = "---\nrequires:\n  - dep-a\n---\n# Skill"
    assert parse_skill_requires(md) == ["dep-a"]


def test_parse_skill_requires_multiple_deps():
    md = "---\nrequires:\n  - dep-a\n  - dep-b\n  - dep-c\n---\n# Skill"
    assert parse_skill_requires(md) == ["dep-a", "dep-b", "dep-c"]


def test_parse_skill_requires_stops_at_non_list_line():
    md = "---\nrequires:\n  - dep-a\nname: foo\n---\n"
    assert parse_skill_requires(md) == ["dep-a"]


def test_parse_skill_requires_strips_whitespace():
    md = "---\nrequires:\n  -   spaced  \n---\n"
    assert parse_skill_requires(md) == ["spaced"]


# ── find_skill_md ───────────────────────────────────────────────────


def test_find_skill_md_direct_file_path(tmp_path: Path):
    skill_file = tmp_path / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# My Skill")
    assert find_skill_md(str(skill_file), tmp_path) == skill_file


def test_find_skill_md_skill_md_parent_exists_but_file_missing(tmp_path: Path):
    (tmp_path / "my-skill").mkdir()
    missing = tmp_path / "my-skill" / "SKILL.md"
    assert find_skill_md(str(missing), tmp_path) is None


def test_find_skill_md_by_name_single_dir(tmp_path: Path):
    skill_file = tmp_path / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# My Skill")
    assert find_skill_md("my-skill", tmp_path) == skill_file


def test_find_skill_md_by_name_multi_dir(tmp_path: Path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    skill_file = dir_b / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# My Skill")
    assert find_skill_md("my-skill", [dir_a, dir_b]) == skill_file


def test_find_skill_md_not_found(tmp_path: Path):
    assert find_skill_md("nonexistent", tmp_path) is None


# ── resolve_dependencies ────────────────────────────────────────────


def test_resolve_dependencies_no_deps(tmp_path: Path):
    (tmp_path / "a" / "SKILL.md").parent.mkdir()
    (tmp_path / "a" / "SKILL.md").write_text("# A")
    assert resolve_dependencies(["a"], skills_dir=tmp_path) == ["a"]


def test_resolve_dependencies_with_chain(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "SKILL.md").write_text("---\nrequires:\n  - b\n---\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "SKILL.md").write_text("# B")
    assert resolve_dependencies(["a"], skills_dir=tmp_path) == ["b", "a"]


def test_resolve_dependencies_deduplicates(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "SKILL.md").write_text("---\nrequires:\n  - c\n---\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "SKILL.md").write_text("---\nrequires:\n  - c\n---\n")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "SKILL.md").write_text("# C")
    assert resolve_dependencies(["a", "b"], skills_dir=tmp_path) == ["c", "a", "b"]


def test_resolve_dependencies_missing_skill(tmp_path: Path):
    assert resolve_dependencies(["missing"], skills_dir=tmp_path) == ["missing"]


# ── SkillSelectionResult ────────────────────────────────────────────


def test_skill_selection_result_defaults():
    result = SkillSelectionResult(skills=["a"])
    assert result.lifecycle_skill == ""
    assert result.ask_user is False


# ── SkillLoadingPolicy.lifecycle_for_status ─────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("not_started", "t3-ticket"),
        ("started", "t3-code"),
        ("coded", "t3-test"),
        ("tested", "t3-review"),
        ("reviewed", "t3-ship"),
        ("shipped", "t3-debug"),
        ("unknown_status", ""),
    ],
)
def test_lifecycle_for_status(status, expected):
    assert SkillLoadingPolicy.lifecycle_for_status(status) == expected


# ── SkillLoadingPolicy.lifecycle_for_phase ──────────────────────────


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("ticket-intake", "t3-ticket"),
        ("coding", "t3-code"),
        ("testing", "t3-test"),
        ("reviewing", "t3-review"),
        ("shipping", "t3-ship"),
        ("debugging", "t3-debug"),
        ("requesting_review", "t3-review-request"),
        ("retrospecting", "t3-retro"),
        ("nonexistent", ""),
    ],
)
def test_lifecycle_for_phase(phase, expected):
    assert SkillLoadingPolicy.lifecycle_for_phase(phase) == expected


# ── SkillLoadingPolicy.lifecycle_for_task_text ──────────────────────


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("debug the issue", "t3-debug"),
        ("fix it now", "t3-debug"),
        ("run the tests", "t3-test"),
        ("commit and push", "t3-ship"),
        ("review the code", "t3-review"),
        ("start working on ticket", "t3-ticket"),
        ("do a retro", "t3-retro"),
        ("setup worktree", "t3-workspace"),
        ("nothing matches here xyz", ""),
    ],
)
def test_lifecycle_for_task_text(task, expected):
    assert SkillLoadingPolicy.lifecycle_for_task_text(task) == expected


# ── SkillLoadingPolicy.select_for_agent_launch ──────────────────────


def _make_policy(tmp_path: Path) -> SkillLoadingPolicy:
    return SkillLoadingPolicy(skills_dir=tmp_path)


def _launch(policy, tmp_path, **overrides):
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
    policy = _make_policy(tmp_path)
    with pytest.raises(ValueError, match="--phase and --skill cannot be used together"):
        _launch(policy, tmp_path, explicit_phase="coding", explicit_skills=["t3-test"])


def test_select_for_agent_launch_unknown_phase_raises(tmp_path: Path):
    policy = _make_policy(tmp_path)
    with pytest.raises(ValueError, match="Unknown phase: banana"):
        _launch(policy, tmp_path, explicit_phase="banana")


def test_select_for_agent_launch_explicit_phase(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = _launch(policy, tmp_path, explicit_phase="coding")
    assert result.lifecycle_skill == "t3-code"
    assert "t3-code" in result.skills
    assert result.ask_user is False


def test_select_for_agent_launch_explicit_skills(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = _launch(policy, tmp_path, explicit_skills=["t3-test", "t3-debug"])
    assert result.skills == ["t3-test", "t3-debug"]
    assert result.lifecycle_skill == ""
    assert result.ask_user is False


def test_select_for_agent_launch_ticket_status(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = _launch(policy, tmp_path, ticket_status="coded")
    assert result.lifecycle_skill == "t3-test"
    assert "t3-test" in result.skills


def test_select_for_agent_launch_task_text(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = _launch(policy, tmp_path, task="commit the changes")
    assert result.lifecycle_skill == "t3-ship"


def test_select_for_agent_launch_no_inputs_asks_user(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = _launch(policy, tmp_path)
    assert result.ask_user is True


def test_select_for_agent_launch_ask_user_when_no_lifecycle_no_explicit(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = _launch(policy, tmp_path, task="nothing matches xyz blah")
    assert result.ask_user is True
    assert result.lifecycle_skill == ""


def test_select_for_agent_launch_overlay_active(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = _launch(
        policy,
        tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme"},
        overlay_active=True,
        task="debug this",
    )
    assert "t3-acme" in result.skills
    assert "t3-debug" in result.skills


# ── SkillLoadingPolicy.select_for_prompt_hook ───────────────────────


def test_select_for_prompt_hook_basic(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="t3-code",
        overlay_skill_metadata={},
        loaded_skills=set(),
    )
    assert "t3-code" in result.skills
    assert result.lifecycle_skill == "t3-code"


def test_select_for_prompt_hook_filters_loaded(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="t3-code",
        overlay_skill_metadata={},
        loaded_skills={"t3-code"},
    )
    assert "t3-code" not in result.skills


def test_select_for_prompt_hook_with_supplementary(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="t3-code",
        overlay_skill_metadata={},
        loaded_skills=set(),
        supplementary_skills=["t3-rules", "t3-platforms"],
    )
    assert "t3-rules" in result.skills
    assert "t3-platforms" in result.skills


def test_select_for_prompt_hook_no_intent(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy.select_for_prompt_hook(
        cwd=tmp_path,
        intent="",
        overlay_skill_metadata={},
        loaded_skills=set(),
    )
    assert result.lifecycle_skill == ""


# ── SkillLoadingPolicy.select_for_runtime_phase ────────────────────


def test_select_for_runtime_phase_known(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy.select_for_runtime_phase(
        cwd=tmp_path,
        phase="testing",
        overlay_skill_metadata={},
    )
    assert result.lifecycle_skill == "t3-test"
    assert "t3-test" in result.skills


def test_select_for_runtime_phase_unknown(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy.select_for_runtime_phase(
        cwd=tmp_path,
        phase="unknown-phase",
        overlay_skill_metadata={},
    )
    assert result.lifecycle_skill == ""
    assert result.skills == []


def test_select_for_runtime_phase_with_overlay(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy.select_for_runtime_phase(
        cwd=tmp_path,
        phase="coding",
        overlay_skill_metadata={"skill_path": "t3-overlay"},
    )
    assert result.lifecycle_skill == "t3-code"


# ── _overlay_skill_for_context ──────────────────────────────────────


def test_overlay_no_skill_path(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={},
        overlay_active=False,
        lifecycle_skill="t3-code",
    )
    assert result == ""


def test_overlay_empty_skill_path(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "  "},
        overlay_active=False,
        lifecycle_skill="t3-code",
    )
    assert result == ""


def test_overlay_active_returns_skill_path(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme"},
        overlay_active=True,
        lifecycle_skill="t3-code",
    )
    assert result == "t3-acme"


def test_overlay_no_lifecycle_returns_empty(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*acme*"]},
        overlay_active=False,
        lifecycle_skill="",
    )
    assert result == ""


def test_overlay_remote_patterns_not_a_list(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": "not-a-list"},
        overlay_active=False,
        lifecycle_skill="t3-code",
    )
    assert result == ""


def test_overlay_remote_patterns_empty_list(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": []},
        overlay_active=False,
        lifecycle_skill="t3-code",
    )
    assert result == ""


def test_overlay_remote_patterns_with_non_string_entries(tmp_path: Path):
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": [123, None, ""]},
        overlay_active=False,
        lifecycle_skill="t3-code",
    )
    assert result == ""


def test_overlay_remote_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_matches_any_remote",
        staticmethod(lambda _cwd, _patterns: True),
    )
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*acme*"]},
        overlay_active=False,
        lifecycle_skill="t3-code",
    )
    assert result == "t3-acme"


def test_overlay_remote_no_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_matches_any_remote",
        staticmethod(lambda _cwd, _patterns: False),
    )
    policy = _make_policy(tmp_path)
    result = policy._overlay_skill_for_context(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme", "remote_patterns": ["*acme*"]},
        overlay_active=False,
        lifecycle_skill="t3-code",
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


# ── _resolve_and_dedupe ─────────────────────────────────────────────


def test_resolve_and_dedupe_filters_adopting_ruff(tmp_path: Path):
    (tmp_path / "ac-adopting-ruff").mkdir()
    (tmp_path / "ac-adopting-ruff" / "SKILL.md").write_text("# Ruff")
    (tmp_path / "my-skill").mkdir()
    (tmp_path / "my-skill" / "SKILL.md").write_text("---\nrequires:\n  - ac-adopting-ruff\n---\n")
    policy = _make_policy(tmp_path)
    result = policy._resolve_and_dedupe(["my-skill"])
    assert "ac-adopting-ruff" not in result
    assert "my-skill" in result


def test_resolve_and_dedupe_empty_input(tmp_path: Path):
    policy = _make_policy(tmp_path)
    assert policy._resolve_and_dedupe([]) == []


def test_resolve_and_dedupe_deduplicates(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teetree.skill_loading.resolve_dependencies",
        lambda skills, **_kw: ["a", "a", "b"],
    )
    policy = _make_policy(tmp_path)
    result = policy._resolve_and_dedupe(["a", "a", "b"])
    assert result == ["a", "b"]


# ── _matches_any_remote ─────────────────────────────────────────────


def test_matches_any_remote_true(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_urls",
        staticmethod(lambda _cwd: ["git@github.com:acme/repo.git"]),
    )
    assert SkillLoadingPolicy._matches_any_remote(tmp_path, ["*acme*"]) is True


def test_matches_any_remote_false(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_urls",
        staticmethod(lambda _cwd: ["git@github.com:other/repo.git"]),
    )
    assert SkillLoadingPolicy._matches_any_remote(tmp_path, ["*acme*"]) is False


def test_matches_any_remote_no_urls(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_urls",
        staticmethod(lambda _cwd: []),
    )
    assert SkillLoadingPolicy._matches_any_remote(tmp_path, ["*acme*"]) is False


# ── _git_remote_urls ────────────────────────────────────────────────


def _mock_remote_url(origin_url):
    def _fake(cwd, remote_name):
        if remote_name == "origin":
            return origin_url
        return ""

    return _fake


def test_git_remote_urls_with_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_url",
        staticmethod(_mock_remote_url("git@github.com:acme/repo.git")),
    )
    assert SkillLoadingPolicy._git_remote_urls(tmp_path) == ["git@github.com:acme/repo.git"]


def test_git_remote_urls_fallback_to_remote_v(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_url",
        staticmethod(_mock_remote_url("")),
    )
    monkeypatch.setattr(
        "teetree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(
            args=[],
            returncode=0,
            stdout="upstream\tgit@github.com:acme/repo.git (fetch)\nupstream\tgit@github.com:acme/repo.git (push)\n",
        ),
    )
    result = SkillLoadingPolicy._git_remote_urls(tmp_path)
    assert result == ["git@github.com:acme/repo.git"]


def test_git_remote_urls_fallback_multiple_remotes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_url",
        staticmethod(_mock_remote_url("")),
    )
    monkeypatch.setattr(
        "teetree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(
            args=[],
            returncode=0,
            stdout="fork\tgit@github.com:me/repo.git (fetch)\nupstream\tgit@github.com:acme/repo.git (fetch)\n",
        ),
    )
    result = SkillLoadingPolicy._git_remote_urls(tmp_path)
    assert result == ["git@github.com:me/repo.git", "git@github.com:acme/repo.git"]


def test_git_remote_urls_fallback_oserror(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_url",
        staticmethod(_mock_remote_url("")),
    )
    monkeypatch.setattr(
        "teetree.skill_loading.subprocess.run",
        _raise_oserror,
    )
    assert SkillLoadingPolicy._git_remote_urls(tmp_path) == []


def test_git_remote_urls_fallback_nonzero_returncode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        SkillLoadingPolicy,
        "_git_remote_url",
        staticmethod(_mock_remote_url("")),
    )
    monkeypatch.setattr(
        "teetree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal"),
    )
    assert SkillLoadingPolicy._git_remote_urls(tmp_path) == []


# ── _git_remote_url ─────────────────────────────────────────────────


def test_git_remote_url_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teetree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(
            args=[],
            returncode=0,
            stdout="git@github.com:acme/repo.git\n",
        ),
    )
    assert SkillLoadingPolicy._git_remote_url(tmp_path, "origin") == "git@github.com:acme/repo.git"


def test_git_remote_url_oserror(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teetree.skill_loading.subprocess.run",
        _raise_oserror,
    )
    assert SkillLoadingPolicy._git_remote_url(tmp_path, "origin") == ""


def test_git_remote_url_nonzero_returncode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "teetree.skill_loading.subprocess.run",
        lambda *_a, **_kw: CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )
    assert SkillLoadingPolicy._git_remote_url(tmp_path, "origin") == ""
