"""Tests for the #3100 assignee-assignability resolver (mirrors ``pr_assignee``)."""

from dataclasses import dataclass
from unittest.mock import patch

from teatree.core.merge.pr_assignee import resolve_pr_assignee


@dataclass
class StubHost:
    login: str
    assignable: bool
    probed: list[tuple[str, str]] | None = None

    def current_user(self) -> str:
        return self.login

    def is_assignable(self, *, repo: str, login: str) -> bool:
        if self.probed is not None:
            self.probed.append((repo, login))
        return self.assignable


class TestResolvePrAssignee:
    def test_keeps_assignable_login(self) -> None:
        host = StubHost(login="souliane", assignable=True)

        assert resolve_pr_assignee(host, repo="souliane/teatree") == "souliane"

    def test_drops_non_assignable_login(self) -> None:
        host = StubHost(login="pullonly-bot", assignable=False)

        assert resolve_pr_assignee(host, repo="souliane/teatree") == ""

    def test_falls_back_to_git_user_name(self) -> None:
        host = StubHost(login="", assignable=True, probed=[])

        with patch("teatree.utils.git.config_value", return_value="gituser"):
            assert resolve_pr_assignee(host, repo="souliane/teatree") == "gituser"
        assert host.probed == [("souliane/teatree", "gituser")]

    def test_empty_when_no_identity_at_all(self) -> None:
        host = StubHost(login="", assignable=True, probed=[])

        with patch("teatree.utils.git.config_value", return_value=""):
            assert resolve_pr_assignee(host, repo="souliane/teatree") == ""
        assert host.probed == []
