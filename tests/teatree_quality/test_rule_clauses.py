"""Rule-clause preservation: a RULES.md edit may not silently drop a clause."""

from pathlib import Path

import pytest

from teatree.quality.rule_clauses import (
    RULES_PATH,
    RuleClause,
    missing_clauses,
    removal_names,
    rule_clauses,
    rules_diff,
)
from tests._git_repo import make_git_repo, run_git

OLD = """# Rules

Intro sentence here.

0. **Alpha rule:** First clause one. Second clause two.

1. **Beta rule:** Third clause three; fourth clause four.
"""


def _texts(clauses: tuple[RuleClause, ...]) -> list[str]:
    return [clause.text for clause in clauses]


class TestRuleClauses:
    def test_each_sentence_of_a_numbered_rule_is_one_clause_under_its_title(self) -> None:
        clauses = rule_clauses(OLD)

        assert [(c.entry, c.text) for c in clauses if c.entry == "Alpha rule"] == [
            ("Alpha rule", "First clause one."),
            ("Alpha rule", "Second clause two."),
        ]

    def test_a_bullet_is_a_clause_of_its_own(self) -> None:
        clauses = rule_clauses("## Section\n\n- Keep the door shut.\n- Keep the window shut.\n")

        assert _texts(clauses) == ["Keep the door shut.", "Keep the window shut."]


class TestMissingClauses:
    def test_a_dropped_clause_is_missing(self) -> None:
        new = OLD.replace(" Second clause two.", "")

        assert _texts(missing_clauses(OLD, new, [])) == ["Second clause two."]

    def test_a_reworded_clause_is_missing(self) -> None:
        new = OLD.replace("Second clause two.", "Second clause 2.")

        assert _texts(missing_clauses(OLD, new, [])) == ["Second clause two."]

    def test_a_clause_moved_to_another_rule_is_missing(self) -> None:
        new = OLD.replace(" Second clause two.", "").replace(
            "Third clause three;", "Third clause three; Second clause two."
        )

        assert _texts(missing_clauses(OLD, new, [])) == ["Second clause two."]

    def test_every_clause_kept_with_an_addition_passes(self) -> None:
        new = OLD.replace("Second clause two.", "Second clause two. A brand new clause.")

        assert missing_clauses(OLD, new, []) == ()

    def test_a_removal_named_by_its_text_passes(self) -> None:
        new = OLD.replace(" Second clause two.", "")

        assert missing_clauses(OLD, new, ["Second clause two."]) == ()

    def test_a_removal_named_by_the_rule_title_passes_every_clause_of_that_rule(self) -> None:
        new = OLD.replace("1. **Beta rule:** Third clause three; fourth clause four.", "")

        assert _texts(missing_clauses(OLD, new, ["Beta rule"])) == []

    def test_a_removal_naming_a_different_clause_does_not_excuse_this_one(self) -> None:
        new = OLD.replace(" Second clause two.", "")

        assert _texts(missing_clauses(OLD, new, ["First clause one."])) == ["Second clause two."]

    def test_a_too_short_name_excuses_nothing(self) -> None:
        new = OLD.replace(" Second clause two.", "")

        assert _texts(missing_clauses(OLD, new, ["clause"])) == ["Second clause two."]


class TestRemovalNames:
    def test_reads_every_trailer_across_commit_messages(self) -> None:
        messages = [
            "fix(rules): tighten\n\nBody.\n\nRule-clause-removed: Second clause two.\nRule-clause-removed: Beta rule",
            "chore: unrelated\n\nNo trailer here.",
        ]

        assert removal_names(messages) == ("Second clause two.", "Beta rule")


def _rules_branch(tmp_path: Path, *, new_rules: str, message: str) -> Path:
    repo = make_git_repo(tmp_path / "repo")
    rules = repo / RULES_PATH
    rules.parent.mkdir(parents=True)
    rules.write_text(OLD, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "seed rules")
    run_git(repo, "checkout", "-q", "-b", "feature")
    rules.write_text(new_rules, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return repo


class TestRulesDiff:
    def test_a_branch_that_drops_an_unnamed_clause_is_refused_naming_it(self, tmp_path: Path) -> None:
        repo = _rules_branch(tmp_path, new_rules=OLD.replace(" Second clause two.", ""), message="docs(rules): trim")

        diff = rules_diff(str(repo), base="main", head="feature")

        assert diff.touched
        assert _texts(diff.missing) == ["Second clause two."]
        assert "Second clause two." in (diff.refusal() or "")
        assert "Rule-clause-removed:" in (diff.refusal() or "")

    def test_a_trailer_naming_the_dropped_clause_passes(self, tmp_path: Path) -> None:
        repo = _rules_branch(
            tmp_path,
            new_rules=OLD.replace(" Second clause two.", ""),
            message="docs(rules): trim\n\nRule-clause-removed: Second clause two.",
        )

        diff = rules_diff(str(repo), base="main", head="feature")

        assert diff.touched
        assert diff.refusal() is None

    def test_a_branch_keeping_every_clause_passes(self, tmp_path: Path) -> None:
        repo = _rules_branch(
            tmp_path, new_rules=OLD + "\n2. **Gamma rule:** Added clause.\n", message="docs(rules): add"
        )

        assert rules_diff(str(repo), base="main", head="feature").refusal() is None

    def test_a_branch_not_touching_the_rules_file_is_untouched(self, tmp_path: Path) -> None:
        repo = _rules_branch(tmp_path, new_rules=OLD, message="docs(rules): noop")
        (repo / "other.txt").write_text("x\n", encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "chore: other")

        diff = rules_diff(str(repo), base="main", head="feature")

        assert not diff.touched
        assert diff.refusal() is None

    def test_a_deleted_rules_file_misses_every_clause(self, tmp_path: Path) -> None:
        repo = _rules_branch(tmp_path, new_rules=OLD, message="docs(rules): noop")
        (repo / RULES_PATH).unlink()
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "docs(rules): delete")

        diff = rules_diff(str(repo), base="main", head="feature")

        assert len(diff.missing) == len(rule_clauses(OLD))

    def test_an_unreadable_base_ref_fails_loud(self, tmp_path: Path) -> None:
        repo = _rules_branch(tmp_path, new_rules=OLD, message="docs(rules): noop")

        with pytest.raises(RuntimeError, match="no-such-base"):
            rules_diff(str(repo), base="no-such-base", head="feature")
