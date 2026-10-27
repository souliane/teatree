import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from teatree.backends.gitlab.http_client import GitLabHTTPClient
from teatree.config import cold_reader
from teatree.config.cold_reader import SettingRead
from teatree.utils import secrets


def _response(method: str = "GET") -> httpx.Response:
    return httpx.Response(200, json={}, request=httpx.Request(method, "https://gitlab.example/api/v4/projects/42"))


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("projects/Group%2Frepo/issues?state=opened", "READ"),
        ("/projects/42/merge_requests", "NUMERIC"),
        ("projects/group%2Fother/issues", "CLIENT"),
        ("groups/Group%2Frepo/projects", "CLIENT"),
    ],
)
def test_get_uses_only_matching_project_read_token(
    endpoint: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cold_reader,
        "read_setting_confirmed",
        lambda _key: SettingRead(value={"group/REPO": "project-entry", "42": "numeric-entry"}, readable=True),
    )
    monkeypatch.setattr(
        secrets, "read_pass", lambda entry: {"project-entry": "READ", "numeric-entry": "NUMERIC"}[entry]
    )
    request = MagicMock(return_value=_response())
    monkeypatch.setattr(httpx, "get", request)

    GitLabHTTPClient(token="CLIENT").get_json(endpoint)

    assert request.call_args.kwargs["headers"] == {"PRIVATE-TOKEN": expected}


@pytest.mark.parametrize(
    "failure",
    ["setting-exception", "store-unreadable", "bad-setting", "secret-exception", "empty-secret", "bad-entry"],
)
def test_failed_read_token_lookup_warns_and_uses_client_token(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def read_setting(_key: str) -> SettingRead:
        if failure == "setting-exception":
            message = "store unavailable"
            raise RuntimeError(message)
        if failure == "store-unreadable":
            return SettingRead(value=None, readable=False)
        if failure == "bad-setting":
            return SettingRead(value=["private-entry"], readable=True)
        return SettingRead(value={"group/repo": 7 if failure == "bad-entry" else "private-entry"}, readable=True)

    def read_secret(_entry: str) -> str:
        if failure == "secret-exception":
            message = "secret unavailable"
            raise RuntimeError(message)
        return "" if failure == "empty-secret" else "READ"

    monkeypatch.setattr(cold_reader, "read_setting_confirmed", read_setting)
    monkeypatch.setattr(secrets, "read_pass", read_secret)
    request = MagicMock(return_value=_response())
    monkeypatch.setattr(httpx, "get", request)

    with caplog.at_level(logging.WARNING, logger="teatree.backends.gitlab.http_client"):
        GitLabHTTPClient(token="CLIENT").get_json("projects/group%2Frepo/issues")

    assert request.call_args.kwargs["headers"] == {"PRIVATE-TOKEN": "CLIENT"}
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "group/repo" in warnings[0]
    assert "private-entry" not in warnings[0]
    assert "READ" not in warnings[0]
    assert "CLIENT" not in warnings[0]


def test_unconfigured_read_token_keeps_client_token_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cold_reader, "read_setting_confirmed", lambda _key: SettingRead(value=None, readable=True))
    request = MagicMock(return_value=_response())
    monkeypatch.setattr(httpx, "get", request)

    with caplog.at_level(logging.WARNING, logger="teatree.backends.gitlab.http_client"):
        GitLabHTTPClient(token="CLIENT").get_json("projects/group%2Frepo/issues")

    assert request.call_args.kwargs["headers"] == {"PRIVATE-TOKEN": "CLIENT"}
    assert not caplog.records


def test_matching_gets_read_secret_once_per_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cold_reader, "read_setting_confirmed", lambda _key: SettingRead(value={"42": "entry"}, readable=True)
    )
    read_secret = MagicMock(return_value="READ")
    monkeypatch.setattr(secrets, "read_pass", read_secret)
    request = MagicMock(return_value=_response())
    monkeypatch.setattr(httpx, "get", request)
    client = GitLabHTTPClient(token="CLIENT")

    client.get_json("projects/42/issues")
    client.get_json("projects/42/merge_requests")

    assert [call.kwargs["headers"] for call in request.call_args_list] == [
        {"PRIVATE-TOKEN": "READ"},
        {"PRIVATE-TOKEN": "READ"},
    ]
    read_secret.assert_called_once_with("entry")


def test_writes_keep_client_token_when_project_has_read_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cold_reader, "read_setting_confirmed", lambda _key: SettingRead(value={"42": "entry"}, readable=True)
    )
    monkeypatch.setattr(secrets, "read_pass", lambda _entry: "READ")
    requests = {method: MagicMock(return_value=_response(method.upper())) for method in ("post", "put", "delete")}
    for method, request in requests.items():
        monkeypatch.setattr(httpx, method, request)
    upload = tmp_path / "sample.txt"
    upload.write_text("content")
    client = GitLabHTTPClient(token="CLIENT")

    client.post_json("projects/42/issues", {"title": "test"})
    client.put_json("projects/42/issues/1", {"title": "test"})
    client.delete("projects/42/issues/1")
    client.graphql("mutation { updateIssue(input: {}) { errors } }")
    client.upload_file(42, str(upload))

    assert requests["post"].call_count == 3
    for request in requests.values():
        assert all(call.kwargs["headers"] == {"PRIVATE-TOKEN": "CLIENT"} for call in request.call_args_list)
