"""Tests for the anti-prose lint hook (souliane/teatree#140 Stage 0).

The hook fails when ``skills/**/SKILL.md`` or ``skills/**/references/*.md``
grow new imperative rules (``Non-Negotiable``, leading ``Always``/``Never``
bullets) without an accompanying change in ``src/``, ``hooks/scripts/``, or
``tests/``.
"""

import pytest

from scripts.hooks.check_skill_prose import (
    NEW_RULE_PATTERN,
    count_new_rule_lines,
    has_companion_code_change,
    main,
)

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


class TestHasCompanionCodeChange:
    @pytest.mark.parametrize(
        "files",
        [
            ["src/teatree/core/models/ticket.py"],
            ["hooks/scripts/hook_router.py"],
            ["tests/test_check_skill_prose.py"],
            ["src/teatree/cli/setup.py", "skills/ship/SKILL.md"],
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
            [],
        ],
    )
    def test_returns_false_for_doc_only(self, files: list[str]) -> None:
        assert has_companion_code_change(files) is False


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
