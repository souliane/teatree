"""GitLab branch tip, merge and malformed-read decisions."""

from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from teatree.backends.gitlab.api import GitLabAPI
from teatree.backends.gitlab.client import GitLabCodeHost
from teatree.backends.gitlab.pr_reads import list_unshipped_branches


def test_merged_head_is_shipped_and_extended_head_is_unshipped() -> None:
    branch = {"name": "5346-work", "commit": {"id": "new"}, "merged": False}
    client = MagicMock()
    project = SimpleNamespace(project_id=42)
    client.get_json_paginated.side_effect = [[branch], [{"sha": "new"}]]
    assert list_unshipped_branches(client, project, repo="souliane/teatree", prefix="5346-") == []
    client.get_json_paginated.side_effect = [[branch], [{"sha": "old"}]]
    assert list_unshipped_branches(client, project, repo="souliane/teatree", prefix="5346-") == ["5346-work"]


def test_non_list_page_raises() -> None:
    client = GitLabAPI(token="x")
    response = MagicMock()
    response.json.return_value = {"message": "error"}
    with patch.object(client, "_get", return_value=response), pytest.raises(TypeError, match="non-list"):
        client.get_json_paginated("projects/42/repository/branches", require_list=True)


def test_http_500_raises_instead_of_reporting_no_branches() -> None:
    client = GitLabAPI(token="x")
    response = httpx.Response(500, request=httpx.Request("GET", "https://gitlab.com/api/v4/projects/42"))
    with patch.object(client, "_get", return_value=response), pytest.raises(httpx.HTTPStatusError):
        client.get_json_paginated("projects/42/repository/branches", require_list=True)


def test_host_preserves_local_qualified_and_bare_project_resolution() -> None:
    client = MagicMock()
    host = GitLabCodeHost(client=client)
    with patch("teatree.backends.gitlab.pr_reads.list_unshipped_branches", return_value=[]):
        with TemporaryDirectory() as directory:
            assert host.list_unshipped_branches(repo=directory, prefix="5346-") == []
            client.resolve_project_from_remote.assert_called_once_with(directory)
        client.reset_mock()
        assert host.list_unshipped_branches(repo="souliane/teatree", prefix="5346-") == []
        client.resolve_project.assert_called_once_with("souliane/teatree")
        client.reset_mock()
        assert host.list_unshipped_branches(repo="teatree", prefix="5346-") == []
        client.resolve_project_from_remote.assert_called_once_with(".")
