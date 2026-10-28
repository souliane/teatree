"""Forge MR classifier verdicts and fail-loud transport behavior."""

import subprocess

import httpx
import pytest

from teatree.backends.gitlab import http_client as gitlab_http
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.slack_broadcast_mr_classifier import ForgeMrStateClassifier, _classifier_head_sha

GH_URL = "https://github.com/team/project/pull/42"
GL_URL = "https://gitlab.example.com/team/group/project/-/merge_requests/7"
API_URL = "https://owned.gitlab.example/api/v4/projects/team%2Fgroup%2Fproject/merge_requests/7"


@pytest.fixture(autouse=True)
def _routed_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab_http, "client_for_url", lambda _url: httpx)

    def resolve(_url: str, *, credential: str) -> ForgeTokenResolution:
        return ForgeTokenResolution(credential, "owner", ForgeTokenState.TOKEN, token=f"routed-{credential}")

    monkeypatch.setattr("teatree.loop.scanners.slack_broadcast_mr_classifier.resolve_url_token", resolve)
    monkeypatch.setattr(
        "teatree.loop.scanners.gitlab_http.overlay_gitlab_base_url",
        lambda _name: "https://owned.gitlab.example/api/v4",
    )
    monkeypatch.setenv("T3_GITLAB_HTTP_MAX_RETRIES", "0")


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["tool"], returncode=returncode, stdout=stdout, stderr=stderr)


def _response(payload: object, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", API_URL))


class TestVerdicts:
    def test_merged_github_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.backends.github.api.run_allowed_to_fail",
            lambda *a, **k: _completed('{"state": "MERGED", "reviewDecision": "APPROVED", "author": {"login": "me"}}'),
        )
        [state] = ForgeMrStateClassifier()([GH_URL])
        assert (state.merged, state.approved, state.author_username) == (True, True, "me")

    def test_open_gitlab_mr_binds_host_token_and_nested_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
            calls.append((url, headers))
            return _response({"state": "opened", "upvotes": 0, "author": {"username": "colleague"}})

        monkeypatch.setattr("httpx.get", get)
        [state] = ForgeMrStateClassifier()([GL_URL])
        assert (state.merged, state.approved, state.author_username) == (False, False, "colleague")
        assert calls == [(API_URL, {"PRIVATE-TOKEN": "routed-gitlab_token"})]

    def test_non_forge_url_is_a_deterministic_non_match(self) -> None:
        [state] = ForgeMrStateClassifier()(["https://example.com/not/an/mr"])
        assert (state.merged, state.approved) == (False, False)


class TestFailureIsNotAVerdict:
    @pytest.mark.parametrize(("url", "credential"), [(GH_URL, "github_token"), (GL_URL, "gitlab_token")])
    def test_empty_route_never_uses_ambient_auth(
        self, monkeypatch: pytest.MonkeyPatch, url: str, credential: str
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(
            "teatree.loop.scanners.slack_broadcast_mr_classifier.resolve_url_token",
            lambda *_a, **_k: ForgeTokenResolution(credential, "owner", ForgeTokenState.UNSET),
        )
        monkeypatch.setattr("httpx.get", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr("teatree.backends.github.api.run_allowed_to_fail", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ScannerError) as exc:
            ForgeMrStateClassifier()([url])
        assert exc.value.error_class is ScannerErrorClass.AUTH
        assert calls == []

    @pytest.mark.parametrize(("url", "credential"), [(GH_URL, "github_token"), (GL_URL, "gitlab_token")])
    def test_token_state_with_empty_value_is_auth_failure(
        self, monkeypatch: pytest.MonkeyPatch, url: str, credential: str
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(
            "teatree.loop.scanners.slack_broadcast_mr_classifier.resolve_url_token",
            lambda *_a, **_k: ForgeTokenResolution(credential, "owner", ForgeTokenState.TOKEN, token=""),
        )
        monkeypatch.setattr("httpx.get", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr("teatree.backends.github.api.run_allowed_to_fail", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ScannerError) as exc:
            ForgeMrStateClassifier()([url])
        assert exc.value.error_class is ScannerErrorClass.AUTH
        assert calls == []

    def test_github_nonzero_return_code_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.backends.github.api.run_allowed_to_fail",
            lambda *a, **k: _completed("", returncode=1, stderr="401 Bad credentials"),
        )
        with pytest.raises(ScannerError) as exc:
            ForgeMrStateClassifier()([GH_URL])
        assert exc.value.error_class is ScannerErrorClass.AUTH

    @pytest.mark.parametrize(
        ("status", "error_class"),
        [
            (401, ScannerErrorClass.AUTH),
            (403, ScannerErrorClass.AUTH),
            (429, ScannerErrorClass.RATE_LIMIT),
            (404, ScannerErrorClass.UNKNOWN),
            (500, ScannerErrorClass.UNKNOWN),
        ],
    )
    def test_gitlab_http_error_class(
        self, monkeypatch: pytest.MonkeyPatch, status: int, error_class: ScannerErrorClass
    ) -> None:
        monkeypatch.setattr("httpx.get", lambda *a, **k: _response({"message": "failed"}, status=status))
        with pytest.raises(ScannerError) as exc:
            ForgeMrStateClassifier()([GL_URL])
        assert exc.value.error_class is error_class

    def test_gitlab_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*_a: object, **_k: object) -> httpx.Response:
            detail = "offline"
            raise httpx.ConnectError(detail)

        monkeypatch.setattr("httpx.get", fail)
        with pytest.raises(ScannerError) as exc:
            ForgeMrStateClassifier()([GL_URL])
        assert exc.value.error_class is ScannerErrorClass.NETWORK

    @pytest.mark.parametrize("body", ["not json at all", "[]", ""])
    def test_gitlab_unreadable_payload(self, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
        response = httpx.Response(200, content=body, request=httpx.Request("GET", API_URL))
        monkeypatch.setattr("httpx.get", lambda *a, **k: response)
        with pytest.raises(ScannerError) as exc:
            ForgeMrStateClassifier()([GL_URL])
        assert exc.value.error_class is ScannerErrorClass.UNKNOWN


class TestClassifierHeadSha:
    def test_prefers_top_level_and_falls_back_to_diff_refs(self) -> None:
        assert _classifier_head_sha({"sha": "top123"}, key="sha") == "top123"
        assert _classifier_head_sha({"diff_refs": {"head_sha": "nested456"}}, key="sha") == "nested456"
        assert _classifier_head_sha({}, key="sha") == ""
