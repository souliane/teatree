"""Tests for skill_loader — intent detection, overlay discovery, dependency resolution."""

from pathlib import Path

import pytest
from lib.skill_loader import (
    build_suggestion,
    detect_intent,
    detect_overlay,
    detect_supplementary_skills,
    detect_url_intent,
    get_skill_deps,
    resolve_companion_skills,
    suggest_skills,
)

# ---------------------------------------------------------------------------
# detect_intent
# ---------------------------------------------------------------------------


class TestDetectIntent:
    """At least one pattern per skill, matching the bash regex parity."""

    # -- t3-ship --
    @pytest.mark.parametrize(
        "prompt",
        [
            "create a merge request",
            "create an MR",
            "push this branch",
            "finalize the work",
            "deliver it now",
            "ship it",
            "create mr",
            "create a PR",
            "pull request for this",
            "commit these changes",
        ],
    )
    def test_ship(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-ship"

    def test_ship_not_review(self) -> None:
        """'review this MR' should NOT trigger t3-ship."""
        assert detect_intent("review this merge request") != "t3-ship"

    def test_commit_not_review(self) -> None:
        """'review commit' should NOT trigger t3-ship."""
        assert detect_intent("review the commit message") != "t3-ship"

    # -- t3-test --
    @pytest.mark.parametrize(
        "prompt",
        [
            "run the tests",
            "run pytest",
            "lint the code",
            "fix sonar issues",
            "run e2e tests",
            "CI failed on the pipeline",
            "the pipeline failed miserably",
            "what tests broke?",
            "pipeline is red",
            "test runner config",
        ],
    )
    def test_test(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-test"

    # -- t3-review-request --
    @pytest.mark.parametrize(
        "prompt",
        [
            "request review from the team",
            "ask for review",
            "send the code for review",
            "notify reviewer about the MR",
            "post mr links",
            "review request batch",
        ],
    )
    def test_review_request(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-review-request"

    # -- t3-review --
    @pytest.mark.parametrize(
        "prompt",
        [
            "review the code",
            "check my code quality",
            "give me feedback on this",
            "quality check this module",
            "do a code review",
        ],
    )
    def test_review(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-review"

    # -- t3-debug --
    @pytest.mark.parametrize(
        "prompt",
        [
            "it's broken",
            "there's an error in the logs",
            "the page is not working",
            "app crash on startup",
            "blank page after login",
            "can't connect to the database",
            "debug this endpoint",
            "fix this issue now",
            "server won't start",
            "getting a 500 error",
            "traceback in the console",
            "exception raised",
        ],
    )
    def test_debug(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-debug"

    # -- t3-ticket --
    @pytest.mark.parametrize(
        "prompt",
        [
            "new ticket from the board",
            "start working on the feature",
            "what should I do next?",
            "PROJ-1234",
            "ticket #456",
            "issue 789",
        ],
    )
    def test_ticket(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-ticket"

    # -- t3-code --
    @pytest.mark.parametrize(
        "prompt",
        [
            "implement the login page",
            "code it up",
            "add a new feature",
            "refactor the utils module",
            "fix the authentication bug",
            "change the database schema",
            "update the API endpoint",
            "remove the old migration",
            "create a new service",
            "Add validation to the form",
            "Extract the helper into a module",
            "rework the caching layer",
            "redesign the data model",
        ],
    )
    def test_code(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-code"

    def test_code_bare_imperative(self) -> None:
        """Bare imperative verb at start triggers t3-code."""
        assert detect_intent("scaffold a new component") == "t3-code"

    # -- t3-setup --
    @pytest.mark.parametrize(
        "prompt",
        [
            "setup skills for this project",
            "configure claude agent",
            "install skills from the repo",
            "bootstrap skills",
            "configure hooks for teatree",
        ],
    )
    def test_setup(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-setup"

    # -- t3-contribute --
    @pytest.mark.parametrize(
        "prompt",
        [
            "t3-contribute the improvements",
            "push improvements to the fork",
            "push skills upstream",
            "contribute upstream to the main repo",
        ],
    )
    def test_contribute(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-contribute"

    # -- t3-retro --
    @pytest.mark.parametrize(
        "prompt",
        [
            "let's do a retro",
            "run a retrospective",
            "lessons learned from this session",
            "let's auto-improve",
            "time for auto-improve",
            "what went wrong?",
        ],
    )
    def test_retro(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-retro"

    # -- t3-followup --
    @pytest.mark.parametrize(
        "prompt",
        [
            "follow-up on my tickets",
            "autopilot mode",
            "batch tickets from the board",
            "process all tickets",
            "not started issues need attention",
            "work on all my tickets",
            "check ticket status",
            "advance tickets",
            "remind reviewers",
            "mr reminders please",
            "nudge the team",
        ],
    )
    def test_followup(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-followup"

    # -- t3-workspace --
    @pytest.mark.parametrize(
        "prompt",
        [
            "create a new worktree",
            "setup the environment",
            "start the server",
            "start session",
            "refresh db",
            "cleanup old worktrees",
            "reset password for dev",
            "restore the database",
            "start the backend server",
            "start the frontend",
            "database is stale",
        ],
    )
    def test_workspace(self, prompt: str) -> None:
        assert detect_intent(prompt) == "t3-workspace"

    # -- no match --
    def test_no_match(self) -> None:
        assert detect_intent("hello, how are you?") == ""

    def test_empty_prompt(self) -> None:
        assert detect_intent("") == ""

    def test_case_insensitive(self) -> None:
        assert detect_intent("Run Pytest NOW") == "t3-test"


# ---------------------------------------------------------------------------
# detect_url_intent
# ---------------------------------------------------------------------------


class TestDetectUrlIntent:
    def test_gitlab_issue(self) -> None:
        assert detect_url_intent("check https://gitlab.example.com/org/repo/-/issues/42") == "t3-ticket"

    def test_gitlab_mr(self) -> None:
        assert detect_url_intent("https://gitlab.example.com/org/repo/-/merge_requests/99") == "t3-ticket"

    def test_gitlab_job(self) -> None:
        assert detect_url_intent("https://gitlab.company.io/team/app/-/jobs/123") == "t3-ticket"

    def test_github_issue(self) -> None:
        assert detect_url_intent("look at https://github.com/owner/repo/issues/77") == "t3-ticket"

    def test_github_pr(self) -> None:
        assert detect_url_intent("https://github.com/owner/repo/pull/55") == "t3-ticket"

    def test_notion(self) -> None:
        assert detect_url_intent("spec at https://www.notion.so/workspace/page-abc") == "t3-ticket"

    def test_notion_site(self) -> None:
        assert detect_url_intent("see https://notion.site/some-page") == "t3-ticket"

    def test_confluence(self) -> None:
        assert detect_url_intent("https://company.atlassian.net/wiki/spaces/X/pages/123") == "t3-ticket"

    def test_linear(self) -> None:
        assert detect_url_intent("https://linear.app/team/issue/TEAM-123") == "t3-ticket"

    def test_sentry(self) -> None:
        assert detect_url_intent("https://sentry.io/organizations/org/issues/456") == "t3-debug"

    def test_self_hosted_sentry(self) -> None:
        assert detect_url_intent("https://my-sentry.company.com/issues/789") == "t3-debug"

    def test_no_url(self) -> None:
        assert detect_url_intent("just some text") == ""

    def test_overlay_url_patterns(self, tmp_path: Path) -> None:
        overlay = tmp_path / "my-overlay"
        hook_config = overlay / "hook-config"
        hook_config.mkdir(parents=True)
        (hook_config / "url-patterns.yml").write_text(
            't3-debug:\n  - "https?://custom-errors\\.example\\.com/"\nt3-ticket:\n  - "https?://my-tracker\\.dev/"\n',
            encoding="utf-8",
        )
        assert detect_url_intent("see https://custom-errors.example.com/err/1", str(overlay)) == "t3-debug"
        assert detect_url_intent("see https://my-tracker.dev/issue/1", str(overlay)) == "t3-ticket"

    def test_overlay_no_match(self, tmp_path: Path) -> None:
        overlay = tmp_path / "my-overlay"
        hook_config = overlay / "hook-config"
        hook_config.mkdir(parents=True)
        (hook_config / "url-patterns.yml").write_text(
            't3-debug:\n  - "https?://custom-errors\\.example\\.com/"\n',
            encoding="utf-8",
        )
        assert detect_url_intent("no urls here", str(overlay)) == ""


# ---------------------------------------------------------------------------
# detect_overlay
# ---------------------------------------------------------------------------


class TestDetectOverlay:
    def test_match_cwd(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "ac-myproject"
        (skill_dir / "hook-config").mkdir(parents=True)
        (skill_dir / "hook-config" / "context-match.yml").write_text(
            'cwd_patterns:\n  - "my-special-project"\n',
            encoding="utf-8",
        )
        result = detect_overlay("/home/user/workspace/my-special-project/src", [], [str(tmp_path / "skills")])
        assert result == "ac-myproject"

    def test_match_active_repo(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "ac-myproject"
        (skill_dir / "hook-config").mkdir(parents=True)
        (skill_dir / "hook-config" / "context-match.yml").write_text(
            "cwd_patterns:\n  - my-backend\n",
            encoding="utf-8",
        )
        result = detect_overlay("/somewhere/else", ["my-backend-repo"], [str(tmp_path / "skills")])
        assert result == "ac-myproject"

    def test_no_match(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "ac-myproject"
        (skill_dir / "hook-config").mkdir(parents=True)
        (skill_dir / "hook-config" / "context-match.yml").write_text(
            "cwd_patterns:\n  - totally-different\n",
            encoding="utf-8",
        )
        result = detect_overlay("/home/user/other", [], [str(tmp_path / "skills")])
        assert result == ""

    def test_no_context_match_file(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "skills" / "ac-bare"
        skill_dir.mkdir(parents=True)
        result = detect_overlay("/anywhere", [], [str(tmp_path / "skills")])
        assert result == ""

    def test_multiple_search_dirs(self, tmp_path: Path) -> None:
        # First dir has no match, second does
        dir1 = tmp_path / "dir1" / "ac-nope"
        (dir1 / "hook-config").mkdir(parents=True)
        (dir1 / "hook-config" / "context-match.yml").write_text(
            "cwd_patterns:\n  - nope\n",
            encoding="utf-8",
        )
        dir2 = tmp_path / "dir2" / "ac-found"
        (dir2 / "hook-config").mkdir(parents=True)
        (dir2 / "hook-config" / "context-match.yml").write_text(
            "cwd_patterns:\n  - my-project\n",
            encoding="utf-8",
        )
        result = detect_overlay("/home/my-project/src", [], [str(tmp_path / "dir1"), str(tmp_path / "dir2")])
        assert result == "ac-found"


# ---------------------------------------------------------------------------
# get_skill_deps
# ---------------------------------------------------------------------------


class TestGetSkillDeps:
    def test_parses_requires(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "t3-code"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: t3-code\nrequires:\n  - t3-workspace\nmetadata:\n  version: 0.0.1\n---\n",
            encoding="utf-8",
        )
        assert get_skill_deps("t3-code", [str(tmp_path)]) == ["t3-workspace"]

    def test_multiple_deps(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "t3-multi"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: t3-multi\nrequires:\n  - t3-workspace\n  - t3-code\n---\n",
            encoding="utf-8",
        )
        assert get_skill_deps("t3-multi", [str(tmp_path)]) == ["t3-workspace", "t3-code"]

    def test_no_requires(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "t3-retro"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: t3-retro\nmetadata:\n  version: 0.0.1\n---\n",
            encoding="utf-8",
        )
        assert get_skill_deps("t3-retro", [str(tmp_path)]) == []

    def test_missing_skill(self, tmp_path: Path) -> None:
        assert get_skill_deps("nonexistent-skill", [str(tmp_path)]) == []

    def test_no_search_dirs(self) -> None:
        assert get_skill_deps("t3-code") == []

    def test_real_skill_files(self) -> None:
        """Verify against actual SKILL.md files in the repo."""
        repo_root = str(Path(__file__).resolve().parent.parent)
        assert get_skill_deps("t3-code", [repo_root]) == ["t3-workspace"]
        assert get_skill_deps("t3-ship", [repo_root]) == ["t3-workspace"]
        assert get_skill_deps("t3-retro", [repo_root]) == []
        assert get_skill_deps("t3-setup", [repo_root]) == []


# ---------------------------------------------------------------------------
# resolve_companion_skills
# ---------------------------------------------------------------------------


class TestResolveCompanionSkills:
    def test_matches_cwd(self, tmp_path: Path) -> None:
        overlay = tmp_path / "ac-myproject"
        (overlay / "hook-config").mkdir(parents=True)
        (overlay / "hook-config" / "context-match.yml").write_text(
            "cwd_patterns:\n"
            "  - my-project\n"
            "companion_skills:\n"
            "  ac-django:\n"
            "    - my-backend\n"
            "  ac-angular:\n"
            "    - my-frontend\n",
            encoding="utf-8",
        )
        result = resolve_companion_skills(str(overlay), "/home/user/my-backend/src", [])
        assert result == ["ac-django"]

    def test_matches_active_repo(self, tmp_path: Path) -> None:
        overlay = tmp_path / "ac-myproject"
        (overlay / "hook-config").mkdir(parents=True)
        (overlay / "hook-config" / "context-match.yml").write_text(
            "companion_skills:\n  ac-django:\n    - my-backend\n",
            encoding="utf-8",
        )
        result = resolve_companion_skills(str(overlay), "/elsewhere", ["my-backend"])
        assert result == ["ac-django"]

    def test_no_companion_section(self, tmp_path: Path) -> None:
        overlay = tmp_path / "ac-myproject"
        (overlay / "hook-config").mkdir(parents=True)
        (overlay / "hook-config" / "context-match.yml").write_text(
            "cwd_patterns:\n  - my-project\n",
            encoding="utf-8",
        )
        result = resolve_companion_skills(str(overlay), "/home/my-project", [])
        assert result == []

    def test_no_match(self, tmp_path: Path) -> None:
        overlay = tmp_path / "ac-myproject"
        (overlay / "hook-config").mkdir(parents=True)
        (overlay / "hook-config" / "context-match.yml").write_text(
            "companion_skills:\n  ac-django:\n    - totally-different\n",
            encoding="utf-8",
        )
        result = resolve_companion_skills(str(overlay), "/not-matching", [])
        assert result == []

    def test_missing_file(self, tmp_path: Path) -> None:
        result = resolve_companion_skills(str(tmp_path / "nonexistent"), "/anywhere", [])
        assert result == []


# ---------------------------------------------------------------------------
# detect_supplementary_skills
# ---------------------------------------------------------------------------


class TestDetectSupplementarySkills:
    def test_matches_pattern(self, tmp_path: Path) -> None:
        config = tmp_path / "skills.yml"
        config.write_text(
            "my-ruff: '\\b(ruff|lint adopt)\\b'\nmy-pdf: '\\b(acroform|pdf template)\\b'\n",
            encoding="utf-8",
        )
        assert detect_supplementary_skills("run ruff check", str(config)) == ["my-ruff"]

    def test_multiple_matches(self, tmp_path: Path) -> None:
        config = tmp_path / "skills.yml"
        config.write_text(
            "skill-a: '\\bfoo\\b'\nskill-b: '\\bbar\\b'\n",
            encoding="utf-8",
        )
        assert detect_supplementary_skills("foo and bar", str(config)) == ["skill-a", "skill-b"]

    def test_no_match(self, tmp_path: Path) -> None:
        config = tmp_path / "skills.yml"
        config.write_text("my-skill: '\\bnope\\b'\n", encoding="utf-8")
        assert detect_supplementary_skills("hello world", str(config)) == []

    def test_missing_config(self, tmp_path: Path) -> None:
        assert detect_supplementary_skills("anything", str(tmp_path / "missing.yml")) == []

    def test_skips_comments(self, tmp_path: Path) -> None:
        config = tmp_path / "skills.yml"
        config.write_text("# comment\n\nmy-skill: '\\bhello\\b'\n", encoding="utf-8")
        assert detect_supplementary_skills("hello", str(config)) == ["my-skill"]


# ---------------------------------------------------------------------------
# build_suggestion
# ---------------------------------------------------------------------------


class TestBuildSuggestion:
    def test_workspace_always_first(self) -> None:
        result = build_suggestion(
            intent="t3-code",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=[],
            supplementary_skills=[],
        )
        assert result[0] == "t3-workspace"
        assert "t3-code" in result

    def test_workspace_not_duplicated_when_intent(self) -> None:
        result = build_suggestion(
            intent="t3-workspace",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=[],
            supplementary_skills=[],
        )
        assert result == ["t3-workspace"]

    def test_skip_workspace_for_retro(self) -> None:
        result = build_suggestion(
            intent="t3-retro",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=[],
            supplementary_skills=[],
        )
        assert "t3-workspace" not in result
        assert result == ["t3-retro"]

    def test_skip_workspace_for_setup(self) -> None:
        result = build_suggestion(
            intent="t3-setup",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=[],
            supplementary_skills=[],
        )
        assert "t3-workspace" not in result
        assert result == ["t3-setup"]

    def test_skip_already_loaded(self) -> None:
        result = build_suggestion(
            intent="t3-code",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=["t3-workspace", "t3-code"],
            supplementary_skills=[],
        )
        assert result == []

    def test_overlay_included_in_project_context(self) -> None:
        result = build_suggestion(
            intent="t3-code",
            project_context=True,
            project_overlay="ac-myproject",
            overlay_skill_dir="",
            loaded_skills=["t3-workspace"],
            supplementary_skills=[],
        )
        assert "t3-code" in result
        assert "ac-myproject" in result

    def test_supplementary_appended(self) -> None:
        result = build_suggestion(
            intent="t3-code",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=["t3-workspace"],
            supplementary_skills=["my-ruff"],
        )
        assert "t3-code" in result
        assert "my-ruff" in result

    def test_no_duplicates(self) -> None:
        result = build_suggestion(
            intent="t3-code",
            project_context=True,
            project_overlay="ac-myproject",
            overlay_skill_dir="",
            loaded_skills=[],
            supplementary_skills=["t3-workspace", "ac-myproject"],
        )
        # Each should appear only once
        assert len(result) == len(set(result))

    def test_deps_resolved(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "t3-test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: t3-test-skill\nrequires:\n  - t3-workspace\n  - t3-extra\n---\n",
            encoding="utf-8",
        )
        result = build_suggestion(
            intent="t3-test-skill",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=[],
            supplementary_skills=[],
            skill_search_dirs=[str(tmp_path)],
        )
        assert "t3-workspace" in result
        assert "t3-test-skill" in result
        assert "t3-extra" in result

    def test_empty_intent(self) -> None:
        result = build_suggestion(
            intent="",
            project_context=False,
            project_overlay="",
            overlay_skill_dir="",
            loaded_skills=[],
            supplementary_skills=[],
        )
        assert result == []


# ---------------------------------------------------------------------------
# suggest_skills (JSON entry point)
# ---------------------------------------------------------------------------


class TestSuggestSkills:
    def test_basic_intent(self) -> None:
        result = suggest_skills({"prompt": "run the tests"})
        assert result["intent"] == "t3-test"
        assert "t3-test" in list(result["suggestions"])  # type: ignore[arg-type]

    def test_project_context_default(self) -> None:
        result = suggest_skills({"prompt": "hello", "project_context": True})
        assert result["intent"] == "t3-code"

    def test_no_intent_no_context(self) -> None:
        result = suggest_skills({"prompt": "hello", "project_context": False})
        assert result["intent"] == ""
        assert result["suggestions"] == []

    def test_url_takes_priority(self) -> None:
        result = suggest_skills({"prompt": "look at https://sentry.io/organizations/org/issues/1"})
        assert result["intent"] == "t3-debug"

    def test_supplementary_config(self, tmp_path: Path) -> None:
        config = tmp_path / "supp.yml"
        config.write_text("my-extra: '\\bmagic\\b'\n", encoding="utf-8")
        result = suggest_skills(
            {
                "prompt": "run the tests with magic",
                "supplementary_config": str(config),
            }
        )
        assert "my-extra" in list(result["suggestions"])  # type: ignore[arg-type]
        assert result["intent"] == "t3-test"


# ---------------------------------------------------------------------------
# Coverage gap tests — filesystem-dependent edge cases
# ---------------------------------------------------------------------------


class TestCheckOverlayUrlPatterns:
    def test_missing_file(self, tmp_path: Path) -> None:
        result = detect_url_intent("check https://custom.io/thing", str(tmp_path / "nonexistent"))
        assert result == ""

    def test_comments_and_blanks(self, tmp_path: Path) -> None:
        hook_dir = tmp_path / "hook-config"
        hook_dir.mkdir()
        (hook_dir / "url-patterns.yml").write_text(
            "# comment\n\nt3-debug:\n  - custom\\.io/issues\n",
            encoding="utf-8",
        )
        result = detect_url_intent("check https://custom.io/issues/1", str(tmp_path))
        assert result == "t3-debug"

    def test_no_match_in_overlay(self, tmp_path: Path) -> None:
        hook_dir = tmp_path / "hook-config"
        hook_dir.mkdir()
        (hook_dir / "url-patterns.yml").write_text("t3-ticket:\n  - nope\\.io\n", encoding="utf-8")
        result = detect_url_intent("check https://other.io/stuff", str(tmp_path))
        assert result == ""


class TestParseCwdPatternsEdges:
    def test_comments_and_section_end(self, tmp_path: Path) -> None:
        from lib.skill_loader import _parse_cwd_patterns  # noqa: PLC0415

        match_file = tmp_path / "context-match.yml"
        content = (
            "# comment line\n\ncwd_patterns:\n  - my-project\n"
            "  - other-project\ncompanion_skills:\n  ac-django:\n    - backend\n"
        )
        match_file.write_text(content, encoding="utf-8")
        patterns = _parse_cwd_patterns(match_file)
        assert patterns == ["my-project", "other-project"]


class TestDetectOverlayEdges:
    def test_nondir_in_search_path(self, tmp_path: Path) -> None:
        """Non-directory entries in skill_search_dirs are skipped."""
        fake_file = tmp_path / "not-a-dir"
        fake_file.write_text("x", encoding="utf-8")
        result = detect_overlay("/some/cwd", [], [str(fake_file)])
        assert result == ""

    def test_file_entry_in_skills_root(self, tmp_path: Path) -> None:
        """Files inside a skills root are skipped (only dirs are candidates)."""
        (tmp_path / "a-file.txt").write_text("x", encoding="utf-8")
        result = detect_overlay("/some/cwd", [], [str(tmp_path)])
        assert result == ""

    def test_dir_without_hook_config(self, tmp_path: Path) -> None:
        """Skill dirs without hook-config/context-match.yml are skipped."""
        (tmp_path / "my-skill").mkdir()
        result = detect_overlay("/some/cwd", [], [str(tmp_path)])
        assert result == ""

    def test_none_search_dirs(self) -> None:
        result = detect_overlay("/some/cwd", [], None)
        assert result == ""


class TestGetSkillDepsEdges:
    def test_content_before_frontmatter(self, tmp_path: Path) -> None:
        """Lines before the first --- are ignored."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "Some preamble text\n---\nname: my-skill\nrequires:\n  - t3-workspace\n---\n",
            encoding="utf-8",
        )
        deps = get_skill_deps("my-skill", [str(tmp_path)])
        assert deps == ["t3-workspace"]


class TestResolveCompanionEdges:
    def test_comments_in_companion_section(self, tmp_path: Path) -> None:
        hook_dir = tmp_path / "hook-config"
        hook_dir.mkdir()
        (hook_dir / "context-match.yml").write_text(
            "companion_skills:\n  # a comment\n\n  ac-django:\n    - backend\n",
            encoding="utf-8",
        )
        result = resolve_companion_skills(str(tmp_path), "/workspace/backend/src", [])
        assert "ac-django" in result


class TestBuildSuggestionEdges:
    def test_project_with_overlay_and_companions(self, tmp_path: Path) -> None:
        """Companions are included when overlay_skill_dir has context-match.yml."""
        hook_dir = tmp_path / "hook-config"
        hook_dir.mkdir()
        (hook_dir / "context-match.yml").write_text(
            "companion_skills:\n  ac-django:\n    - anything\n",
            encoding="utf-8",
        )
        result = build_suggestion(
            intent="t3-code",
            project_context=True,
            project_overlay="ac-oper",
            overlay_skill_dir=str(tmp_path),
            loaded_skills=[],
            supplementary_skills=[],
        )
        assert "ac-oper" in result
        assert "ac-django" not in result  # "anything" not in cwd=""

    def test_project_with_matching_companion(self, tmp_path: Path) -> None:
        hook_dir = tmp_path / "hook-config"
        hook_dir.mkdir()
        # Use empty pattern "" which matches any cwd via `"" in cwd`
        (hook_dir / "context-match.yml").write_text(
            'companion_skills:\n  ac-django:\n    - ""\n',
            encoding="utf-8",
        )
        result = build_suggestion(
            intent="t3-code",
            project_context=True,
            project_overlay="ac-oper",
            overlay_skill_dir=str(tmp_path),
            loaded_skills=[],
            supplementary_skills=[],
        )
        assert "ac-django" in result
