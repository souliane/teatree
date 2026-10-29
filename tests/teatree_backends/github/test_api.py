"""GitHub backend runner credential boundary."""

import json
from collections.abc import Iterator
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import httpx
import pytest

from teatree.backends.forge_pool import close_forge_clients
from teatree.backends.github import api
from teatree.loop.scanners.base import classify_gh_stderr
from teatree.types import ScannerErrorClass
from teatree.utils.run import CommandFailedError
from tests._forge_server import ForgeServer, forge_server


def test_unchecked_runner_refuses_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(api, "run_allowed_to_fail", run)
    result = api.run_gh_unchecked(["pr", "list"], token="")
    assert result.returncode == 4
    run.assert_not_called()


def test_post_accepts_empty_success_body(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=CompletedProcess(["gh"], 0, stdout="", stderr=""))
    monkeypatch.setattr(api, "_run_gh", run)
    assert api._gh_api_post("repos/example/repo/hooks/7/pings", {}, token="test-token") is None
    argv = run.call_args.args
    assert argv[-2:] == ("--input", "-")
    assert run.call_args.kwargs["stdin_text"] == "{}"


def test_delete_sends_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=CompletedProcess(["gh"], 0, stdout="", stderr=""))
    monkeypatch.setattr(api, "_run_gh", run)
    api._gh_api_delete("repos/example/repo/hooks/7", token="test-token")
    argv = run.call_args.args
    assert argv[3:5] == ("--method", "DELETE")
    assert "--input" not in argv
    assert "stdin_text" not in run.call_args.kwargs


@pytest.mark.parametrize(
    ("stdout", "complaint"),
    [
        ('{"message":"unexpected"}', "non-list page set"),
        ('[{"id":7}]', "non-list page"),
        ("[[1]]", "non-object hook"),
    ],
)
def test_strict_paginated_read_rejects_unreadable_response(
    monkeypatch: pytest.MonkeyPatch, stdout: str, complaint: str
) -> None:
    run = Mock(return_value=CompletedProcess(["gh"], 0, stdout=stdout, stderr=""))
    monkeypatch.setattr(api, "_run_gh", run)
    with pytest.raises(ValueError, match=f"{complaint}$"):
        api.gh_api_get_paginated("repos/example/repo/hooks", token="test-token", strict=True)


def test_the_pooled_head_sha_read_returns_just_the_sha() -> None:
    response = httpx.Response(200, json={"sha": "abc123"}, request=httpx.Request("GET", "https://api.github.com/x"))
    with patch.object(httpx.Client, "request", return_value=response):
        result = api._run_gh("gh", "api", "repos/example/repo/commits/main", "--jq", ".sha", token="test-token")
    assert result.stdout == "abc123\n"


def test_an_unsupported_jq_expression_is_refused_instead_of_returning_the_raw_body() -> None:
    with (
        patch.object(httpx.Client, "request") as request,
        pytest.raises(CommandFailedError, match="Unsupported pooled gh api --jq"),
    ):
        api._run_gh("gh", "api", "repos/example/repo/releases/latest", "--jq", ".tag_name", token="test-token")
    request.assert_not_called()


@pytest.fixture
def pooled_server() -> Iterator[ForgeServer]:
    close_forge_clients()
    with (
        forge_server() as server,
        patch.object(api, "_api_url", lambda endpoint: f"{server.base}/{endpoint.lstrip('/')}"),
    ):
        yield server
    close_forge_clients()


def _paginate(endpoint: str) -> CompletedProcess[str]:
    return api._run_gh("gh", "api", endpoint, "--paginate", "--slurp", token="test-token")


def test_pooled_pagination_follows_the_link_header_to_the_next_page(pooled_server: ForgeServer) -> None:
    pooled_server.payloads = {"/items": [1], "/items2": [2]}
    pooled_server.links = {"/items": f'<{pooled_server.base}/items2>; rel="next"'}

    assert json.loads(_paginate("items").stdout) == [[1], [2]]
    assert pooled_server.paths == ["/items", "/items2"]


def test_pooled_pagination_refuses_a_next_page_on_another_host(pooled_server: ForgeServer) -> None:
    pooled_server.links = {"/items": f'<http://127.0.0.2:{pooled_server.server_port}/steal>; rel="next"'}

    with pytest.raises(CommandFailedError, match="crossed forge host"):
        _paginate("items")

    assert pooled_server.paths == ["/items"]


def test_pooled_pagination_stops_at_the_page_cap(pooled_server: ForgeServer) -> None:
    pooled_server.links = {"/loop": f'<{pooled_server.base}/loop>; rel="next"'}

    with pytest.raises(CommandFailedError, match="pagination limit exceeded"):
        _paginate("loop")

    assert len(pooled_server.paths) == 1000


def test_a_missing_resource_keeps_the_http_404_text_that_is_not_found_reads(pooled_server: ForgeServer) -> None:
    pooled_server.statuses = {"/missing": 404}

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "missing", token="test-token")

    assert "HTTP 404" in raised.value.stderr
    assert api.is_not_found(raised.value)


def test_pooled_error_message_cannot_turn_http_403_into_not_found(pooled_server: ForgeServer) -> None:
    pooled_server.statuses["/forbidden"] = 403
    pooled_server.payloads["/forbidden"] = {"message": "quoted HTTP 404 from a previous request"}

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "forbidden", token="test-token")

    assert not api.is_not_found(raised.value)


def test_quota_counter_401_does_not_turn_http_404_into_auth(pooled_server: ForgeServer) -> None:
    pooled_server.statuses["/missing"] = 404
    pooled_server.response_headers["/missing"] = {"X-RateLimit-Remaining": "401"}

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "missing", token="test-token")

    assert classify_gh_stderr(raised.value.stderr) == ScannerErrorClass.UNKNOWN


def test_pooled_rate_limit_keeps_the_response_cause_and_retry_header(pooled_server: ForgeServer) -> None:
    echoed_token = "a" * 20
    pooled_server.statuses["/limited"] = 403
    pooled_server.payloads["/limited"] = {
        "message": f"You have exceeded a secondary rate limit. Authorization: Bearer {echoed_token}"
    }
    pooled_server.response_headers["/limited"] = {"Retry-After": "45", "X-RateLimit-Resource": "search"}

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "limited", token="test-token")

    stderr = raised.value.stderr
    assert "HTTP 403" in stderr
    assert "secondary rate limit" in stderr
    assert "retry-after=45" in stderr
    assert "resource=search" in stderr
    assert echoed_token not in stderr
    assert "Authorization: <redacted>" in stderr
    assert classify_gh_stderr(stderr) == ScannerErrorClass.RATE_LIMIT


def test_pooled_error_redacts_a_quoted_credential_before_shortening_the_message(pooled_server: ForgeServer) -> None:
    pooled_server.statuses["/limited"] = 403
    pooled_server.payloads["/limited"] = {"message": "x" * 170 + ' "token": "PLACEHOLDER-NOT-A-CREDENTIAL"'}

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "limited", token="test-token")

    assert "PLACEHOLD" not in raised.value.stderr
    assert '"token": "<redacted>"' in raised.value.stderr


def test_pooled_error_redacts_a_quoted_credential_before_shortening_a_header(pooled_server: ForgeServer) -> None:
    pooled_server.statuses["/limited"] = 403
    pooled_server.response_headers["/limited"] = {
        "X-RateLimit-Resource": "x" * 20 + ' "token": "PLACEHOLDER-NOT-A-CREDENTIAL"'
    }

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "limited", token="test-token")

    assert "PLACEHOLDER" not in raised.value.stderr
    assert '"token": "<redacted>"' in raised.value.stderr


def test_pooled_blank_error_message_has_no_dangling_separator(pooled_server: ForgeServer) -> None:
    pooled_server.statuses["/limited"] = 403
    pooled_server.payloads["/limited"] = {"message": " \n "}

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "limited", token="test-token")

    assert raised.value.stderr == "gh api: HTTP 403"


@pytest.mark.parametrize(
    ("status", "headers", "diagnostic"),
    [
        (403, {"Retry-After": "45"}, "retry-after=45"),
        (429, {"Retry-After": "45"}, "retry-after=45"),
        (403, {"X-RateLimit-Remaining": "0"}, "remaining=0"),
    ],
)
def test_pooled_rate_limit_header_classifies_without_message(
    pooled_server: ForgeServer, status: int, headers: dict[str, str], diagnostic: str
) -> None:
    pooled_server.statuses["/limited"] = status
    pooled_server.payloads["/limited"] = {"message": "Forbidden"}
    pooled_server.response_headers["/limited"] = headers

    with pytest.raises(CommandFailedError) as raised:
        api._run_gh("gh", "api", "limited", token="test-token")

    assert diagnostic in raised.value.stderr
    assert classify_gh_stderr(raised.value.stderr) == ScannerErrorClass.RATE_LIMIT


@pytest.mark.parametrize("body", ["not JSON", "[]", '{"message": 17}'])
def test_pooled_unreadable_error_body_keeps_the_status(body: str) -> None:
    response = httpx.Response(403, text=body, request=httpx.Request("GET", "https://api.github.com/x"))
    with (
        patch.object(httpx.Client, "request", return_value=response),
        pytest.raises(CommandFailedError) as raised,
    ):
        api._run_gh("gh", "api", "x", token="test-token")

    assert raised.value.stderr == "gh api: HTTP 403"
