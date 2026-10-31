"""CLI surface of ``t3 tool rule-clauses``: exit codes and the named clause on refusal."""

from pathlib import Path

from click.testing import Result
from typer.testing import CliRunner

from teatree.cli import app
from teatree.quality.rule_clauses import RULES_PATH
from tests._git_repo import make_git_repo, run_git

runner = CliRunner()

OLD = "# Rules\n\n0. **Alpha rule:** First clause one. Second clause two.\n"
DROPPED = "# Rules\n\n0. **Alpha rule:** First clause one.\n"


def _branch_editing_rules(tmp_path: Path, *, message: str) -> Path:
    repo = make_git_repo(tmp_path / "repo")
    rules = repo / RULES_PATH
    rules.parent.mkdir(parents=True)
    rules.write_text(OLD, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "seed")
    run_git(repo, "checkout", "-q", "-b", "feature")
    rules.write_text(DROPPED, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return repo


def _invoke(repo: Path) -> Result:
    return runner.invoke(app, ["tool", "rule-clauses", "--repo", str(repo), "--base", "main", "--head", "feature"])


class TestRuleClausesCommand:
    def test_an_unnamed_dropped_clause_exits_one_naming_it(self, tmp_path: Path) -> None:
        result = _invoke(_branch_editing_rules(tmp_path, message="docs(rules): trim"))

        assert result.exit_code == 1
        assert "Second clause two." in result.output

    def test_a_named_removal_exits_zero(self, tmp_path: Path) -> None:
        repo = _branch_editing_rules(tmp_path, message="docs(rules): trim\n\nRule-clause-removed: Second clause two.")

        assert _invoke(repo).exit_code == 0

    def test_an_unreadable_base_exits_two(self, tmp_path: Path) -> None:
        repo = _branch_editing_rules(tmp_path, message="docs(rules): trim")
        result = runner.invoke(app, ["tool", "rule-clauses", "--repo", str(repo), "--base", "no-such-base"])

        assert result.exit_code == 2
