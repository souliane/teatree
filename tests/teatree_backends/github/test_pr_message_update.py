from unittest.mock import patch

import pytest

from teatree.backends.github.client import GitHubCodeHost
from teatree.core.backend_protocols import PrMessage


def test_update_pr_message_patches_and_verifies() -> None:
    host = GitHubCodeHost(token="test-token")
    message = PrMessage("New title", "New body")
    with (
        patch("teatree.backends.github.client._gh_api_patch") as update,
        patch.object(host, "fetch_pr_message", return_value=message),
    ):
        result = host.update_pr_message(slug="acme/widgets", pr_id=7, message=message)

    assert result == message
    update.assert_called_once_with(
        "repos/acme/widgets/pulls/7", {"title": "New title", "body": "New body"}, token="test-token"
    )


def test_update_pr_message_refuses_an_unverified_write() -> None:
    host = GitHubCodeHost(token="test-token")
    with (
        patch("teatree.backends.github.client._gh_api_patch"),
        patch.object(host, "fetch_pr_message", return_value=PrMessage("old", "old")),
        pytest.raises(RuntimeError, match="did not retain"),
    ):
        host.update_pr_message(slug="acme/widgets", pr_id=7, message=PrMessage("New title", "New body"))
