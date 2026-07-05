"""Canonical-resolver SSOT tests for the factory-signal ledger queries (SIG-1).

Pins the two structural guarantees behind #14/#19: exactly ONE
``resolved_repo_key`` at every audit→ledger join site (S1 and S3 compute
identical keys over a production-shaped fixture matrix), and an AST lint proving
no raw ``normalize_repo_slug(clear.slug)`` comparison — the shape that dropped
the dominant workstream-slug self-merge to ``""`` — survives in the module.
"""

import ast
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.utils import timezone

import teatree.core.factory_signal_queries as fsq
from teatree.core.factory_signal_queries import resolved_repo_key
from teatree.core.factory_signals import compute_factory_signals
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.pr_slug_resolution import resolve_pr_repo_slug
from tests.factories import MergeAuditFactory, MergeClearFactory, TicketFactory


def _row(report, provider_id: str):
    return next(r for r in report.signals if r.provider_id == provider_id)


class ResolvedRepoKeyParityTests(TestCase):
    """S1 and S3 join through the SAME ``resolved_repo_key`` symbol — never divergent keys."""

    def setUp(self) -> None:
        self.now = timezone.now()

    def _audit(self, *, pr_id: int, slug: str, repo_slug: str = "", issue_url: str = "", ticket: bool = True):
        merged_at = self.now - timedelta(days=5)
        ticket_obj = None
        if ticket:
            ticket_obj = TicketFactory(issue_url=issue_url) if issue_url else TicketFactory()
        clear = MergeClearFactory(
            ticket=ticket_obj,
            pr_id=pr_id,
            slug=slug,
            issued_at=merged_at - timedelta(hours=1),
            consumed_at=merged_at,
        )
        return MergeAuditFactory(clear=clear, merged_at=merged_at, repo_slug=repo_slug)

    def _matrix(self):
        # The four production-shaped join shapes the canonical resolver must
        # handle identically for S1 and S3 (owner/repo slug, workstream slug via
        # ticket, cross-repo stamped repo_slug, unresolvable ticket-less CLEAR).
        return {
            "owner_repo": self._audit(pr_id=701, slug="owner/repo1"),
            "workstream_ticket": self._audit(
                pr_id=702, slug="702-feat", issue_url="https://github.com/owner/repo2/issues/702"
            ),
            "cross_repo_stamped": self._audit(pr_id=703, slug="703-feat", repo_slug="owner/repo3"),
            "unresolvable": self._audit(pr_id=704, slug="unresolvable-ws", ticket=False),
        }

    def _resolver_that_fails_the_unresolvable(self):
        def resolve_or_raise(clear: object) -> str:
            if getattr(clear, "pr_id", None) == 704:
                msg = "no resolvable repo"
                raise MergePreconditionError(msg)
            return resolve_pr_repo_slug(clear)

        return mock.patch.object(fsq, "resolve_pr_repo_slug", side_effect=resolve_or_raise)

    def test_resolved_repo_key_per_shape(self) -> None:
        audits = self._matrix()
        with self._resolver_that_fails_the_unresolvable():
            assert resolved_repo_key(audits["owner_repo"]) == ("owner/repo1", 701)
            assert resolved_repo_key(audits["workstream_ticket"]) == ("owner/repo2", 702)
            # The merge-time stamp wins over the CLEAR's workstream slug (#19).
            assert resolved_repo_key(audits["cross_repo_stamped"]) == ("owner/repo3", 703)
            # Unresolvable is reported unmatched (None), never joined on a wrong slug.
            assert resolved_repo_key(audits["unresolvable"]) is None

    def test_s1_and_s3_agree_on_matchable_and_unmatched(self) -> None:
        self._matrix()
        with self._resolver_that_fails_the_unresolvable():
            report = compute_factory_signals(now=self.now)
        s1 = _row(report, "first_try_green").evidence
        s3 = _row(report, "review_catch").evidence
        # Identical keys => identical denominator + unmatched count across lanes.
        assert s1["merges"] == s3["merges"] == 3
        assert s1["unmatched_slug"] == s3["unmatched_slug"] == 1


class ResolverLintTests(TestCase):
    """No raw ``normalize_repo_slug(clear.slug)`` comparison survives in the queries module."""

    def _clear_slug_arg(self, node: ast.AST) -> bool:
        # True for ``clear.slug`` / ``audit.clear.slug`` (a CLEAR's raw slug),
        # False for ``ref.slug`` (a RedMrFixAttempt ref — already owner/repo).
        if not (isinstance(node, ast.Attribute) and node.attr == "slug"):
            return False
        base = node.value
        if isinstance(base, ast.Name):
            return "clear" in base.id.lower()
        return isinstance(base, ast.Attribute) and base.attr == "clear"

    def test_no_raw_normalize_repo_slug_of_a_clear_slug(self) -> None:
        source = Path(fsq.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations: list[int] = []
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == "normalize_repo_slug" and call.args and self._clear_slug_arg(call.args[0]):
                violations.append(call.lineno)
        assert not violations, (
            f"raw normalize_repo_slug(clear.slug) survives at line(s) {violations} — resolve UP to owner/repo "
            f"via resolved_repo_key, never strip the workstream slug to '' (the #14 collapse)"
        )
