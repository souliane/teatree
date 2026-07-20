"""Transient-vs-verdict authorship classification for ``review_request_merge_react`` (F5.2).

``_is_self_authored`` is tri-state:

* ``True``  — author RESOLVED to a user identity → close the row (self-authored).
* ``False`` — author resolved to someone else → the colleague react path proceeds.
* ``None``  — author lookup FAILED (raised or empty) → skip the tick WITHOUT
    stamping ``done_at`` so a later tick retries. A transient forge read must
    never permanently close a colleague's merged review-request.
"""

from dataclasses import dataclass

import pytest
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_request_merge_react import _is_self_authored, react_merge_on_post
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate

_USER = "souliane"
_COLLEAGUE = "a-colleague"
_MR_URL = "https://github.com/o/r/pull/7"
_CHANNEL = "C0DEMOCHAN1"
_THREAD_TS = "1780473408.767019"


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


@dataclass
class _Post:
    mr_url: str = _MR_URL


@dataclass
class _Host:
    author: str = ""
    user: str = _USER
    raise_on_author: Exception | None = None

    def current_user(self) -> str:
        return self.user

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        if self.raise_on_author is not None:
            raise self.raise_on_author
        return self.author


class TestIsSelfAuthoredTriState:
    def test_resolved_self_returns_true(self) -> None:
        assert _is_self_authored(_Post(), _Host(author=_USER), (_USER,)) is True

    def test_resolved_colleague_returns_false(self) -> None:
        assert _is_self_authored(_Post(), _Host(author=_COLLEAGUE), (_USER,)) is False

    def test_empty_author_is_unresolved_none(self) -> None:
        # Previously this returned True (fail-closed → permanent close). F5.2:
        # an empty author is a lookup failure, not a verdict → None (skip/retry).
        assert _is_self_authored(_Post(), _Host(author=""), (_USER,)) is None

    def test_raised_lookup_is_unresolved_none(self) -> None:
        host = _Host(author="", raise_on_author=RuntimeError("github 500"))
        assert _is_self_authored(_Post(), host, (_USER,)) is None

    def test_no_self_identity_to_protect_returns_false(self) -> None:
        # No aliases and current_user resolves empty → nothing to protect → the
        # colleague path proceeds (False), never a spurious skip.
        assert _is_self_authored(_Post(), _Host(author="", user=""), ()) is False

    def test_no_host_returns_false(self) -> None:
        assert _is_self_authored(_Post(), None, (_USER,)) is False


@dataclass
class _RecordingSlack:
    reactions: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        self.reactions = []

    def react_routed(self, *, channel: str, ts: str, emoji: str):
        assert self.reactions is not None
        self.reactions.append({"channel": channel, "ts": ts, "emoji": emoji})
        return {"ok": True}


def _seed() -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=_MR_URL,
        slack_channel_id=_CHANNEL,
        slack_thread_ts=_THREAD_TS,
        created_at=timezone.now(),
        last_nag_step=0,
        done_at=None,
    )


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestReactMergeOnPostTransientSkip:
    def test_unresolved_author_returns_none_and_leaves_row_open(self) -> None:
        post = _seed()
        slack = _RecordingSlack()
        host = _Host(author="", user=_USER)

        result = react_merge_on_post(post, slack, host=host, identities=(_USER,))

        assert result is None
        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is None

    def test_raised_lookup_returns_none_and_leaves_row_open(self) -> None:
        post = _seed()
        slack = _RecordingSlack()
        host = _Host(author="", raise_on_author=RuntimeError("boom"), user=_USER)

        result = react_merge_on_post(post, slack, host=host, identities=(_USER,))

        assert result is None
        post.refresh_from_db()
        assert post.done_at is None

    def test_resolved_self_closes_the_row(self) -> None:
        _ = PrOpenState  # imported for parity with the scanner's state model
        post = _seed()
        slack = _RecordingSlack()
        host = _Host(author=_USER, user=_USER)

        signal = react_merge_on_post(post, slack, host=host, identities=(_USER,))

        assert signal is not None
        assert signal.kind == "review_request_merge_react.self_authored"
        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is not None
