from unittest.mock import MagicMock, patch

import pytest

from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.scanners.base import ScannerError
from teatree.loop.scanners.codex_review import GhCodexPrApi
from teatree.loop.scanners.codex_review_gitlab import ForgeCodexPrApi, GlabCodexPrApi

SLUG = "acme/team/project"
HEAD = "a" * 40


def _mr(iid: int, author: str, *, draft: bool = False) -> dict[str, object]:
    return {
        "iid": iid,
        "sha": HEAD,
        "draft": draft,
        "web_url": f"https://gitlab.com/{SLUG}/-/merge_requests/{iid}",
        "title": "Change",
        "author": {"username": author},
    }


def test_gitlab_adapter_reads_only_own_mrs_with_paths_and_head_checks() -> None:
    host = MagicMock(spec=CodeHostBackend)
    host.get_repo.return_value = {"id": 77}
    host.current_user.return_value = "Operator"
    host.list_prs.return_value = [_mr(7, "operator"), _mr(8, "colleague"), _mr(9, "OPERATOR", draft=True)]
    host.get_pr_diff.return_value = [{"new_path": "src/auth/session.py"}]
    host.fetch_required_checks_rollup.return_value = [{"sha": HEAD, "status": "running"}]
    host.fetch_live_head_sha.return_value = HEAD

    with patch.object(GlabCodexPrApi, "_backend", return_value=host):
        listed = GlabCodexPrApi(token="test-token").list_open_self_prs(slug=SLUG)

    assert [pr.number for pr in listed] == [7, 9]
    assert listed[0].author == "operator"
    assert listed[0].changed_files == ("src/auth/session.py",)
    assert listed[0].checks_unsettled is True
    assert listed[1].is_draft is True
    assert listed[1].checks_unsettled is False
    host.list_prs.assert_called_once_with(repo=SLUG, state="open", author="Operator")
    assert host.get_pr_diff.call_count == 2
    host.fetch_required_checks_rollup.assert_called_once()


def test_forge_adapter_refuses_a_repository_without_declared_host() -> None:
    adapter = ForgeCodexPrApi(github=GhCodexPrApi(), gitlab=GlabCodexPrApi())
    with (
        patch("teatree.loop.scanners.codex_review_gitlab.forge_for_repo_slug", return_value=""),
        pytest.raises(ScannerError, match="no forge"),
    ):
        adapter.list_open_self_prs(slug=SLUG)
