"""Tests for the ensure-pr helpers (mirrors ``_ensure_pr``).

Split out of ``test_pr_command`` alongside the ``_ensure_pr`` module
extraction: test files mirror the production module path. The behavioural
``ensure-pr`` command tests (PUSHED_ORPHAN / pre-push-deadlock deferral)
stay in ``test_pr_command`` because they drive ``call_command("pr",
"ensure-pr")`` end to end.
"""

from django.test import TestCase

from teatree.core.management.commands._ensure_pr import slug_from_remote


class TestSlugFromRemote(TestCase):
    def test_github_ssh(self) -> None:
        assert slug_from_remote("git@github.com:souliane/teatree.git") == "souliane/teatree"

    def test_github_https(self) -> None:
        assert slug_from_remote("https://github.com/souliane/teatree.git") == "souliane/teatree"

    def test_gitlab_nested_namespace(self) -> None:
        assert slug_from_remote("git@gitlab.com:acme/team/backend.git") == "acme/team/backend"

    def test_no_dot_git_suffix(self) -> None:
        assert slug_from_remote("https://github.com/souliane/teatree") == "souliane/teatree"

    def test_empty_returns_empty(self) -> None:
        assert slug_from_remote("") == ""
