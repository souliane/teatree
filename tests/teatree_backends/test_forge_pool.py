"""The process-wide forge pool: sized by its setting, and one connection serves the public clients' calls."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from teatree.backends.forge_pool import close_forge_clients, configured_max_connections
from teatree.backends.github import api as github_api
from teatree.backends.gitlab.http_client import GitLabHTTPClient
from tests._forge_server import forge_server

_CALLS = 50


@pytest.fixture(autouse=True)
def _fresh_pool() -> Iterator[None]:
    close_forge_clients()
    yield
    close_forge_clients()


def test_fifty_sequential_gitlab_calls_open_exactly_one_connection() -> None:
    with forge_server() as server:
        client = GitLabHTTPClient(token="test", base_url=f"{server.base}/api/v4")
        for _ in range(_CALLS):
            assert client.get_json("projects/1") == {"ok": True}

        assert server.connections == 1


def test_fifty_sequential_calls_across_three_gitlab_clients_open_exactly_one_connection() -> None:
    with forge_server() as server:
        clients = [GitLabHTTPClient(token="test", base_url=f"{server.base}/api/v4") for _ in range(3)]
        for call in range(_CALLS):
            assert clients[call % 3].get_json("projects/1") == {"ok": True}

        assert server.connections == 1


def test_fifty_sequential_github_calls_open_exactly_one_connection() -> None:
    with forge_server() as server, patch.object(github_api, "_api_url", lambda endpoint: f"{server.base}/{endpoint}"):
        for _ in range(_CALLS):
            assert github_api.gh_api_get("repos/example/repo", token="test") == {"ok": True}

        assert server.connections == 1


def test_the_connection_cap_defaults_to_four() -> None:
    assert configured_max_connections() == 4


@pytest.mark.parametrize(("stored", "expected"), [(1, 1), (16, 16), (99, 16)])
def test_the_connection_cap_is_clamped_to_its_range(stored: int, expected: int) -> None:
    with patch("teatree.backends.forge_pool.cold_reader.int_setting", return_value=stored):
        assert configured_max_connections() == expected
