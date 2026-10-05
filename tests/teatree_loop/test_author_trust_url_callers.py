"""Every author-trust caller hands its URL to the decision (#1773).

The visibility of a repo is asked about the repo under its forge host, and a bare ``owner/repo`` gains
that host only from the caller's URL. Each test below declares a repo private ONLY under its host, so a
caller that stops passing its URL asks the probe about a bare slug — and, where the repo decides the
verdict, reaches the other verdict.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock, patch

from django.test import TestCase

from teatree.core.review import author_trust
from teatree.core.review.stranger_pr import pr_is_admitted
from teatree.loop.mechanical import payload_author_untrusted_public
from teatree.loop.scanners import slack_broadcasts
from teatree.loop.scanners.issue_intake import author_is_trusted

_GITLAB_MR = "https://gitlab.example.test/acme/widget/-/merge_requests/7"
_GITLAB_ISSUE = "https://gitlab.example.test/acme/widget/-/issues/7"
_QUALIFIED = "gitlab.example.test/acme/widget"


class TestEveryCallerHandsItsUrlToTheDecision(TestCase):
    """A repo known private only under its own host is recognised only when the caller's URL reaches the probe.

    So blanking the URL any of these callers passes changes what the probe is asked — and, where the
    repo decides the verdict, the verdict itself.
    """

    @contextmanager
    def _private_only_under_its_host(self) -> Iterator[list[str]]:
        asked: list[str] = []

        def visibility(slug: str, *_args: object, **_kwargs: object) -> str | None:
            asked.append(slug)
            return "PRIVATE" if slug == _QUALIFIED else None

        with (
            patch("teatree.hooks._private_repo_entries.private_repo_visibility", side_effect=visibility),
            patch.object(author_trust, "trusted_handles", return_value=set()),
        ):
            yield asked

    def test_the_mechanical_belt_trusts_a_colleague_on_a_private_repo(self) -> None:
        with self._private_only_under_its_host() as asked:
            assert payload_author_untrusted_public({"url": _GITLAB_MR, "author": "colleague"}) is False
        assert asked == [_QUALIFIED]

    def test_a_slack_broadcast_counts_a_colleague_on_a_private_repo_as_trusted(self) -> None:
        scanner = slack_broadcasts.SlackBroadcastsScanner(
            backend=Mock(), channels=(), fetch_channel_history=Mock(), classify_mrs=Mock()
        )
        state = slack_broadcasts.MrState(url=_GITLAB_MR, merged=False, approved=False, author_username="colleague")

        with self._private_only_under_its_host() as asked:
            assert scanner._author_is_trusted(state) is True
        assert asked == [_QUALIFIED]

    def test_issue_intake_asks_about_the_repo_under_its_host(self) -> None:
        issue = {"web_url": _GITLAB_ISSUE, "author": {"username": "colleague"}}

        with self._private_only_under_its_host() as asked:
            assert author_is_trusted(issue, frozenset()) is False
        assert asked == [_QUALIFIED]

    def test_stranger_pr_admission_asks_about_the_repo_under_its_host(self) -> None:
        pr = {"author": {"username": "colleague"}, "labels": []}

        with self._private_only_under_its_host() as asked:
            assert pr_is_admitted(pr, pr_url=_GITLAB_MR, trusted=frozenset(), admit_label="t3-auto") is False
        assert asked == [_QUALIFIED]
