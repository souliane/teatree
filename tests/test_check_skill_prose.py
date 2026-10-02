"""Tests for the anti-prose lint hook (souliane/teatree#140 Stage 0).

The hook fails when a ``**/skills/**/SKILL.md`` or ``**/skills/**/references/*.md``
grows new imperative rules (``Non-Negotiable``, leading ``Always``/``Never``
bullets) without an accompanying change under ``src/``, ``tests/``,
``hooks/scripts/`` or ``scripts/hooks/``.

Every pattern is anchored at ``(?:^|/)`` rather than at the repo root, so one
implementation serves a flat checkout and one that mounts its skills under
``overlay/skills/`` or ``vendor/<core>/skills/``. These tests carry both layouts
so a root-anchored regression turns them red.
"""

import subprocess

import pytest

from scripts.hooks.check_skill_prose import NEW_RULE_PATTERN, count_new_rule_lines, has_companion_code_change, main

_NEW_NN_DIFF = """\
diff --git a/skills/ship/SKILL.md b/skills/ship/SKILL.md
--- a/skills/ship/SKILL.md
+++ b/skills/ship/SKILL.md
@@ -50,0 +51,2 @@
+- **New rule (Non-Negotiable).** Run `prek install` before every commit.
+- More prose.
"""

_NEW_ALWAYS_DIFF = """\
diff --git a/skills/ship/SKILL.md b/skills/ship/SKILL.md
--- a/skills/ship/SKILL.md
+++ b/skills/ship/SKILL.md
@@ -50,0 +51,2 @@
+- **Always run `prek install`** before the first commit in a worktree.
+- **Never push to default branch.** Use a feature branch instead.
"""

_REMOVING_PROSE_DIFF = """\
diff --git a/skills/ship/SKILL.md b/skills/ship/SKILL.md
--- a/skills/ship/SKILL.md
+++ b/skills/ship/SKILL.md
@@ -50,3 +50,0 @@
-- **Old rule (Non-Negotiable).** Removed.
-- **Always do this** thing that became code.
-- **Never do this** other thing.
"""

_REFERENCE_FILE_DIFF = """\
diff --git a/skills/ship/references/foo.md b/skills/ship/references/foo.md
--- a/skills/ship/references/foo.md
+++ b/skills/ship/references/foo.md
@@ -10,0 +11 @@
+- **Non-Negotiable.** New rule in a reference doc.
"""

_NON_SKILL_FILE_DIFF = """\
diff --git a/docs/notes.md b/docs/notes.md
--- a/docs/notes.md
+++ b/docs/notes.md
@@ -10,0 +11 @@
+- **Always be polite.** Non-Negotiable.
"""

_OVERLAY_SKILL_DIFF = """\
diff --git a/overlay/skills/t3-widget/references/playbook.md b/overlay/skills/t3-widget/references/playbook.md
--- a/overlay/skills/t3-widget/references/playbook.md
+++ b/overlay/skills/t3-widget/references/playbook.md
@@ -10,0 +11 @@
+- **Always re-provision before running the suite (Non-Negotiable).**
"""

_VENDORED_SKILL_DIFF = """\
diff --git a/vendor/teatree/skills/ship/SKILL.md b/vendor/teatree/skills/ship/SKILL.md
--- a/vendor/teatree/skills/ship/SKILL.md
+++ b/vendor/teatree/skills/ship/SKILL.md
@@ -50,0 +51 @@
+- **Never force-push a shared branch.** Open a new one instead.
"""

# The path is the whole point of the fixture below — the gate must exclude a
# skill-shaped catalogue that is test data. Hoisted so the diff header stays
# under the line limit without altering the path it asserts on.
_FIXTURE_SKILL_PATH = "evals/fixtures/skill_catalog/skills/ac-python/SKILL.md"

_FIXTURE_SKILL_DIFF = f"""\
diff --git a/{_FIXTURE_SKILL_PATH} b/{_FIXTURE_SKILL_PATH}
--- a/{_FIXTURE_SKILL_PATH}
+++ b/{_FIXTURE_SKILL_PATH}
@@ -10,0 +11 @@
+- **Always annotate every parameter (Non-Negotiable).**
"""


class TestNewRulePattern:
    @pytest.mark.parametrize(
        "line",
        [
            "- **Run `prek install` (Non-Negotiable).**",
            "- **Always assign to user.**",
            "- **Never push to default branch.**",
            "- **Stop on red CI** before retrying.",
        ],
    )
    def test_matches_imperative_bullets(self, line: str) -> None:
        assert NEW_RULE_PATTERN.search(line) is not None

    @pytest.mark.parametrize(
        "line",
        [
            "Some context about why this matters.",
            "- See `references/foo.md` for the full list.",
            "- always lower-case prose, no leading bold marker",
            "- **Background:** the system used to never enforce this.",
        ],
    )
    def test_skips_non_imperative_text(self, line: str) -> None:
        assert NEW_RULE_PATTERN.search(line) is None


class TestCountNewRuleLines:
    def test_counts_non_negotiable(self) -> None:
        result = count_new_rule_lines(_NEW_NN_DIFF)
        assert any(item.path.endswith("SKILL.md") for item in result)
        assert any("Non-Negotiable" in item.line for item in result)

    def test_counts_always_never(self) -> None:
        result = count_new_rule_lines(_NEW_ALWAYS_DIFF)
        assert len(result) == 2
        assert all(item.path.endswith("SKILL.md") for item in result)

    def test_ignores_removed_lines(self) -> None:
        result = count_new_rule_lines(_REMOVING_PROSE_DIFF)
        assert result == []

    def test_includes_reference_files(self) -> None:
        result = count_new_rule_lines(_REFERENCE_FILE_DIFF)
        assert len(result) == 1
        assert "references/" in result[0].path

    def test_skips_non_skill_files(self) -> None:
        result = count_new_rule_lines(_NON_SKILL_FILE_DIFF)
        assert result == []

    @pytest.mark.parametrize("diff", [_OVERLAY_SKILL_DIFF, _VENDORED_SKILL_DIFF])
    def test_counts_skills_mounted_below_the_repo_root(self, diff: str) -> None:
        assert len(count_new_rule_lines(diff)) == 1

    def test_skips_a_skill_shaped_test_fixture(self) -> None:
        # A fixture skill catalogue is test data, not authored prose — the
        # (?:^|/) anchoring reaches it, so it must be excluded by path.
        assert count_new_rule_lines(_FIXTURE_SKILL_DIFF) == []


class TestHasCompanionCodeChange:
    @pytest.mark.parametrize(
        "files",
        [
            ["src/teatree/core/models/ticket.py"],
            ["hooks/scripts/hook_router.py"],
            ["tests/test_check_skill_prose.py"],
            ["src/teatree/cli/setup.py", "skills/ship/SKILL.md"],
            ["vendor/teatree/src/teatree/core/models/ticket.py"],
            ["vendor/teatree/hooks/scripts/hook_router.py"],
            ["overlay/tests/test_overlay_config.py"],
            ["scripts/hooks/check_skill_prose.py"],
        ],
    )
    def test_returns_true_for_code_paths(self, files: list[str]) -> None:
        assert has_companion_code_change(files) is True

    @pytest.mark.parametrize(
        "files",
        [
            ["skills/ship/SKILL.md"],
            ["skills/ship/SKILL.md", "skills/test/SKILL.md"],
            ["skills/ship/references/foo.md"],
            ["docs/notes.md"],
            ["overlay/skills/t3-widget/SKILL.md"],
            ["vendor/teatree/skills/ship/references/foo.md"],
            [],
        ],
    )
    def test_returns_false_for_doc_only(self, files: list[str]) -> None:
        assert has_companion_code_change(files) is False


class TestStagedDiffSelection:
    """The pathspec must be a strict SUPERSET of what the regex then selects.

    A pathspec narrower than the regex short-circuits ``main`` before the regex
    is ever consulted — the failure mode that made this gate return 0 while a
    violation sat staged.
    """

    def test_pathspec_selects_every_markdown_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_skill_prose as mod  # noqa: PLC0415

        captured: list[list[str]] = []

        def _capture(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", _capture)
        mod._staged_diff()
        assert captured[0][captured[0].index("--") + 1 :] == ["*.md"]


class TestMain:
    def test_passes_when_no_skill_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_skill_prose as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: "")
        monkeypatch.setattr(mod, "_staged_files", list)
        assert main() == 0

    def test_fails_when_new_rule_without_companion_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_skill_prose as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: _NEW_NN_DIFF)
        monkeypatch.setattr(mod, "_staged_files", lambda: ["skills/ship/SKILL.md"])
        assert main() == 1

    def test_passes_when_new_rule_has_companion_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_skill_prose as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: _NEW_NN_DIFF)
        monkeypatch.setattr(
            mod,
            "_staged_files",
            lambda: ["skills/ship/SKILL.md", "src/teatree/core/models/ticket.py"],
        )
        assert main() == 0

    def test_passes_when_only_removing_prose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_skill_prose as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda: _REMOVING_PROSE_DIFF)
        monkeypatch.setattr(mod, "_staged_files", lambda: ["skills/ship/SKILL.md"])
        assert main() == 0
