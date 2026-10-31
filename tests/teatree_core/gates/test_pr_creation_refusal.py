"""PR creation refuses a RULES.md diff that drops an unnamed clause and owes a rubric row for any RULES.md edit."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands._ensure_pr import create_or_defer_pr
from teatree.core.models import Rubric, Ticket
from teatree.quality.rule_clauses import RUBRIC_ROW, RULES_PATH
from teatree.utils.run import run_checked
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY
from tests.teatree_core.test_ensure_pr import NonAssignableHost

BRANCH = "docs/9-rules"
REMOTE = "git@github.com:souliane/teatree.git"
OLD_RULES = "# Rules\n\n0. **Alpha rule:** First clause one. Second clause two.\n"
DROPPED = "# Rules\n\n0. **Alpha rule:** First clause one.\n"
GROWN = "# Rules\n\n0. **Alpha rule:** First clause one. Second clause two. Third clause three.\n"
PR_URL = "https://github.com/souliane/teatree/pull/9999"


def _rules_repo(tmp_path: Path, *, edited: str | None, message: str = "docs(rules): edit") -> Path:
    origin = tmp_path / "origin.git"
    run_checked(["git", "init", "--bare", str(origin)])
    work = tmp_path / "work"
    run_checked(["git", "init", "-b", "main", str(work)])
    run_checked(["git", "config", "user.email", "t@example.com"], cwd=work)
    run_checked(["git", "config", "user.name", "agent"], cwd=work)
    run_checked(["git", "remote", "add", "origin", str(origin)], cwd=work)
    rules = work / RULES_PATH
    rules.parent.mkdir(parents=True)
    rules.write_text(OLD_RULES)
    run_checked(["git", "add", "-A"], cwd=work)
    run_checked(["git", "commit", "-m", "seed"], cwd=work)
    run_checked(["git", "push", "-u", "origin", "main"], cwd=work)
    run_checked(["git", "checkout", "-b", BRANCH], cwd=work)
    if edited is None:
        (work / "other.py").write_text("x = 1\n")
    else:
        rules.write_text(edited)
    run_checked(["git", "add", "-A"], cwd=work)
    run_checked(["git", "commit", "-m", message], cwd=work)
    return work


def _spec(repo: Path) -> PullRequestSpec:
    return PullRequestSpec(
        repo=str(repo), branch=BRANCH, title="docs(rules): edit", description="Body.", target_branch="main"
    )


class TestRuleClauseRefusalAtCreation(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_a, **_k: False)

    def test_a_dropped_unnamed_clause_is_refused_before_the_forge_create(self) -> None:
        repo = _rules_repo(self._tmp_path, edited=DROPPED)
        host = NonAssignableHost()

        result = ensure_pr_mod._open_pr(host, _spec(repo), remote=REMOTE, owning_ticket=None)

        assert "Second clause two." in result.get("error", "")
        assert host.created_specs == []

    def test_the_refusal_stands_end_to_end_through_create_or_defer_pr(self) -> None:
        repo = _rules_repo(self._tmp_path, edited=DROPPED)
        host = NonAssignableHost()
        with (
            patch.object(ensure_pr_mod, "code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            result = create_or_defer_pr(str(repo), BRANCH)

        assert "Second clause two." in result.get("error", "")
        assert host.created_specs == []

    def test_a_trailer_naming_the_dropped_clause_lets_the_pr_open(self) -> None:
        repo = _rules_repo(
            self._tmp_path, edited=DROPPED, message="docs(rules): trim\n\nRule-clause-removed: Second clause two."
        )
        host = NonAssignableHost()

        result = ensure_pr_mod._open_pr(host, _spec(repo), remote=REMOTE, owning_ticket=None)

        assert result.get("url") == PR_URL
        assert len(host.created_specs) == 1

    def test_keeping_every_clause_lets_the_pr_open(self) -> None:
        repo = _rules_repo(self._tmp_path, edited=GROWN)
        host = NonAssignableHost()

        result = ensure_pr_mod._open_pr(host, _spec(repo), remote=REMOTE, owning_ticket=None)

        assert result.get("url") == PR_URL

    def test_a_path_that_is_not_a_repository_does_not_block_creation(self) -> None:
        host = NonAssignableHost()
        spec = PullRequestSpec(repo=str(self._tmp_path / "missing"), branch=BRANCH, title="t", description="d")

        result = ensure_pr_mod._open_pr(host, spec, remote=REMOTE, owning_ticket=None)

        assert result.get("url") == PR_URL


class TestRulesRubricRow(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        monkeypatch.setattr("teatree.core.gates.privacy_gate._target_is_public", lambda *_a, **_k: False)

    def _criteria(self, ticket: Ticket) -> list[str]:
        rubric = Rubric.objects.active_for_ticket(ticket)
        return [] if rubric is None else list(rubric.criteria.values_list("text", flat=True))

    def test_a_pr_touching_the_rules_file_owes_the_clause_row(self) -> None:
        repo = _rules_repo(self._tmp_path, edited=GROWN)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/souliane/teatree/issues/9")

        ensure_pr_mod._open_pr(NonAssignableHost(), _spec(repo), remote=REMOTE, owning_ticket=ticket)

        assert self._criteria(ticket) == [RUBRIC_ROW]

    def test_a_pr_not_touching_the_rules_file_owes_no_row(self) -> None:
        repo = _rules_repo(self._tmp_path, edited=None)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/souliane/teatree/issues/9")

        ensure_pr_mod._open_pr(NonAssignableHost(), _spec(repo), remote=REMOTE, owning_ticket=ticket)

        assert self._criteria(ticket) == []
