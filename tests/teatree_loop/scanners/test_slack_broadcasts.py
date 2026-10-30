"""Fault-isolation + transient-vs-verdict tests for ``slack_broadcasts`` (F5.3, F5.5).

Two invariants beyond the classification tests in
``tests/teatree_loop/test_slack_broadcasts_classify.py``:

* **F5.3** — ``ForgeMrStateClassifier`` must distinguish a *verdict*
    (an HTTP or GitHub CLI read found the MR open → ``merged=False``) from a
    *failure to reach a verdict* (binary missing / bad token / rc≠0 / garbage
    output → :class:`ScannerError`). Classifying an unreadable — possibly
    MERGED — MR as open would nag reviewers about already-landed work.
* **F5.5** — one channel's fetch/handle failure must not starve the channels
    queued after it (per-channel isolation), while a classifier
    :class:`ScannerError` and the DB-not-migrated errors still propagate.
"""

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from unittest.mock import patch

import httpx
import pytest
from django.core.cache import cache
from django.test import TestCase

from teatree.backends.gitlab import http_client as gitlab_http
from teatree.core.models import ScannedBroadcast
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.slack_broadcast_mr_classifier import ForgeMrStateClassifier
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

_GITLAB_MR = "https://gitlab.com/acme/app/-/merge_requests/7"
_GITHUB_PR = "https://github.com/souliane/teatree/pull/7"
_RUN = "teatree.backends.github.api.run_allowed_to_fail"


def _completed(*, returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


def _http_response(body: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=body,
        request=httpx.Request("GET", "https://gitlab.com/api/v4/projects/acme%2Fapp/merge_requests/7"),
    )


class TestClassifierTransientVsVerdict:
    """F5.3: a failure to reach a verdict raises; a real "not merged" verdict does not."""

    @pytest.fixture(autouse=True)
    def _routed_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gitlab_http, "client_for_url", lambda _url: httpx)
        monkeypatch.setenv("T3_GITLAB_HTTP_MAX_RETRIES", "0")
        monkeypatch.setattr(
            "teatree.loop.scanners.slack_broadcast_mr_classifier.resolve_url_token",
            lambda _url, *, credential: ForgeTokenResolution(
                credential, "test", ForgeTokenState.TOKEN, token="routed-token"
            ),
        )

    def test_parseable_not_merged_stays_merged_false(self) -> None:
        classifier = ForgeMrStateClassifier()
        with patch("httpx.get", return_value=_http_response('{"state": "opened", "upvotes": 0}')):
            [state] = classifier([_GITLAB_MR])
        assert state.merged is False
        assert state.approved is False

    def test_parseable_merged_is_a_verdict(self) -> None:
        classifier = ForgeMrStateClassifier()
        with patch("httpx.get", return_value=_http_response('{"state": "merged", "upvotes": 2}')):
            [state] = classifier([_GITLAB_MR])
        assert state.merged is True
        assert state.approved is True

    def test_nonzero_rc_raises_scanner_error(self) -> None:
        classifier = ForgeMrStateClassifier()
        with (
            patch("httpx.get", return_value=_http_response('{"message": "failed"}', status=500)),
            pytest.raises(ScannerError) as exc,
        ):
            classifier([_GITLAB_MR])
        assert exc.value.scanner == "slack_broadcasts"

    def test_expired_token_classifies_auth(self) -> None:
        classifier = ForgeMrStateClassifier()
        with (
            patch(_RUN, return_value=_completed(returncode=1, stderr="401 Bad credentials")),
            pytest.raises(ScannerError) as exc,
        ):
            classifier([_GITHUB_PR])
        assert exc.value.error_class is ScannerErrorClass.AUTH

    def test_missing_binary_raises_scanner_error(self) -> None:
        classifier = ForgeMrStateClassifier()
        with patch(_RUN, side_effect=FileNotFoundError("gh")), pytest.raises(ScannerError):
            classifier([_GITHUB_PR])

    def test_garbage_json_raises_scanner_error(self) -> None:
        classifier = ForgeMrStateClassifier()
        with (
            patch("httpx.get", return_value=_http_response("<html>not json</html>")),
            pytest.raises(
                ScannerError,
            ),
        ):
            classifier([_GITLAB_MR])

    def test_empty_output_raises_scanner_error(self) -> None:
        classifier = ForgeMrStateClassifier()
        with patch(_RUN, return_value=_completed(returncode=0, stdout="   ")), pytest.raises(ScannerError):
            classifier([_GITHUB_PR])

    def test_non_object_json_raises_scanner_error(self) -> None:
        classifier = ForgeMrStateClassifier()
        with patch(_RUN, return_value=_completed(returncode=0, stdout="[1, 2, 3]")), pytest.raises(ScannerError):
            classifier([_GITHUB_PR])

    def test_unrecognised_forge_stays_merged_false_not_an_error(self) -> None:
        # A non-GitLab/GitHub URL is a deterministic non-match, not a transient
        # failure — it must not raise.
        classifier = ForgeMrStateClassifier()
        [state] = classifier(["https://example.com/not/a/pr"])
        assert state.merged is False


@dataclass
class _FakeBackend:
    user_id: str = "UUSER"


def _classifier(states_by_url: dict[str, MrState]) -> Callable[[Sequence[str]], list[MrState]]:
    def classify(urls: Sequence[str]) -> list[MrState]:
        return [states_by_url.get(url, MrState(url=url, merged=False, approved=False)) for url in urls]

    return classify


@dataclass
class _ChannelHistory:
    """Per-channel fetcher; a channel id in *raises_for* raises on fetch."""

    by_channel: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    raises_for: frozenset[str] = frozenset()
    seen: list[str] = field(default_factory=list)

    def __call__(self, *, channel: str) -> list[RawAPIDict]:
        self.seen.append(channel)
        if channel in self.raises_for:
            msg = f"fetch failed for {channel}"
            raise RuntimeError(msg)
        return list(self.by_channel.get(channel, ()))


def _msg(text: str, ts: str) -> RawAPIDict:
    return {"text": text, "ts": ts, "type": "message"}


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestPerChannelIsolation:
    """F5.5: a failing channel is logged and skipped; later channels still run."""

    def test_failing_channel_does_not_starve_later_channels(self) -> None:
        fetch = _ChannelHistory(
            by_channel={"C_GOOD": [_msg(f"review {_GITHUB_PR}", "1.1")]},
            raises_for=frozenset({"C_BAD"}),
        )
        scanner = SlackBroadcastsScanner(
            backend=_FakeBackend(),
            channels=["C_BAD", "C_GOOD"],
            fetch_channel_history=fetch,
            classify_mrs=_classifier({_GITHUB_PR: MrState(url=_GITHUB_PR, merged=False, approved=False)}),
        )

        signals = scanner.scan()

        # Both channels were attempted (the bad one did not abort the loop) and
        # the good channel still produced its review-intent signal.
        assert fetch.seen == ["C_BAD", "C_GOOD"]
        assert any(s.kind == "slack.review_intent" for s in signals)

    def test_classifier_scanner_error_propagates(self) -> None:
        # A classifier auth failure is scanner-wide, not a per-channel fault:
        # it must surface to the dispatcher (#1287), not be swallowed.
        def _raise(_urls: Sequence[str]) -> list[MrState]:
            raise ScannerError(scanner="slack_broadcasts", error_class=ScannerErrorClass.AUTH, detail="401")

        scanner = SlackBroadcastsScanner(
            backend=_FakeBackend(),
            channels=["C_A", "C_B"],
            fetch_channel_history=_ChannelHistory(by_channel={"C_A": [_msg(f"review {_GITHUB_PR}", "1.1")]}),
            classify_mrs=_raise,
        )

        with pytest.raises(ScannerError):
            scanner.scan()

    def test_all_healthy_channels_scan_normally(self) -> None:
        fetch = _ChannelHistory(
            by_channel={
                "C_A": [_msg(f"review {_GITHUB_PR}", "1.1")],
                "C_B": [_msg(f"review {_GITLAB_MR}", "2.1")],
            },
        )
        scanner = SlackBroadcastsScanner(
            backend=_FakeBackend(),
            channels=["C_A", "C_B"],
            fetch_channel_history=fetch,
            classify_mrs=_classifier(
                {
                    _GITHUB_PR: MrState(url=_GITHUB_PR, merged=False, approved=False),
                    _GITLAB_MR: MrState(url=_GITLAB_MR, merged=False, approved=False),
                },
            ),
        )

        signals = scanner.scan()

        assert fetch.seen == ["C_A", "C_B"]
        assert len([s for s in signals if s.kind == "slack.review_intent"]) == 2


class TestUnreadableBroadcastLinks(TestCase):
    def test_unreadable_project_does_not_hide_readable_link_and_backs_off(self) -> None:
        unreadable = "https://gitlab.example.com/team/inaccessible/-/merge_requests/72"
        seen: list[str] = []

        def classify(urls: Sequence[str]) -> list[MrState]:
            assert len(urls) == 1
            url = urls[0]
            seen.append(url)
            if url == unreadable:
                response = httpx.Response(404, request=httpx.Request("GET", url))
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ScannerError(
                        scanner="slack_broadcasts", error_class=ScannerErrorClass.UNKNOWN, detail=str(exc)
                    ) from exc
            return [MrState(url=url, merged=False, approved=False)]

        scanner = SlackBroadcastsScanner(
            backend=_FakeBackend(),
            channels=["C_A"],
            fetch_channel_history=_ChannelHistory(
                by_channel={"C_A": [_msg(f"review {_GITHUB_PR} {unreadable}", "72.1")]}
            ),
            classify_mrs=classify,
        )

        first = scanner.scan()
        second = scanner.scan()

        assert seen == [_GITHUB_PR, unreadable, _GITHUB_PR]
        for signals in (first, second):
            assert [signal.kind for signal in signals].count("slack.review_intent") == 1
            health = [signal for signal in signals if signal.kind == "slack.broadcast_unreadable_links"]
            assert len(health) == 1
            assert health[0].payload["repositories"] == {"team/inaccessible": 1}
            assert dispatch(health)[0].zone == "action_needed"

    def test_link_is_retried_after_backoff_and_a_success_clears_the_warning(self) -> None:
        url = "https://gitlab.example.com/team/recovered/-/merge_requests/75"
        attempts = 0

        def classify(urls: Sequence[str]) -> list[MrState]:
            nonlocal attempts
            assert urls == [url]
            attempts += 1
            if attempts == 1:
                response = httpx.Response(404, request=httpx.Request("GET", url))
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ScannerError(
                        scanner="slack_broadcasts", error_class=ScannerErrorClass.UNKNOWN, detail=str(exc)
                    ) from exc
            return [MrState(url=url, merged=True, approved=True)]

        scanner = SlackBroadcastsScanner(
            backend=_FakeBackend(),
            channels=["C_A"],
            fetch_channel_history=_ChannelHistory(by_channel={"C_A": [_msg(f"merged {url}", "75.1")]}),
            classify_mrs=classify,
        )

        first = scanner.scan()
        cache.clear()
        with patch.object(SlackBroadcastsScanner, "_react_done"):
            second = scanner.scan()

        assert attempts == 2
        assert [signal.kind for signal in first] == ["slack.broadcast_unreadable_links"]
        assert not any(signal.kind == "slack.broadcast_unreadable_links" for signal in second)
        assert ScannedBroadcast.objects.get(channel="C_A", slack_ts="75.1").classification == "all_merged"

    def test_unreadable_link_cannot_complete_all_merged(self) -> None:
        unreadable = "https://gitlab.example.com/team/hidden/-/merge_requests/73"

        def classify(urls: Sequence[str]) -> list[MrState]:
            assert len(urls) == 1
            url = urls[0]
            if url == unreadable:
                response = httpx.Response(403, request=httpx.Request("GET", url))
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ScannerError(
                        scanner="slack_broadcasts", error_class=ScannerErrorClass.AUTH, detail=str(exc)
                    ) from exc
            return [MrState(url=url, merged=True, approved=True)]

        scanner = SlackBroadcastsScanner(
            backend=_FakeBackend(),
            channels=["C_A"],
            fetch_channel_history=_ChannelHistory(
                by_channel={"C_A": [_msg(f"merged {_GITHUB_PR} {unreadable}", "73.1")]}
            ),
            classify_mrs=classify,
        )

        signals = scanner.scan()

        assert ScannedBroadcast.objects.get(channel="C_A", slack_ts="73.1").classification == "pending"
        assert [signal.kind for signal in signals] == ["slack.broadcast_unreadable_links"]

    def test_scope_and_server_failures_still_raise(self) -> None:
        for status, body in ((401, "token expired"), (403, "missing_scope: api"), (500, "server error")):
            with self.subTest(status=status):
                unreadable = f"https://gitlab.example.com/team/fault/-/merge_requests/{status}"

                def classify(urls: Sequence[str], status: int = status, body: str = body) -> list[MrState]:
                    assert len(urls) == 1
                    response = httpx.Response(status, text=body, request=httpx.Request("GET", urls[0]))
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise ScannerError(
                            scanner="slack_broadcasts", error_class=ScannerErrorClass.AUTH, detail=str(exc)
                        ) from exc
                    return []

                scanner = SlackBroadcastsScanner(
                    backend=_FakeBackend(),
                    channels=["C_A"],
                    fetch_channel_history=_ChannelHistory(by_channel={"C_A": [_msg(f"review {unreadable}", "74.1")]}),
                    classify_mrs=classify,
                )

                with pytest.raises(ScannerError):
                    scanner.scan()
