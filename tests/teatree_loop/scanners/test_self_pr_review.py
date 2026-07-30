"""Tests for :class:`ClaudeSelfPrReviewScanner` — Claude self-PR review dispatch (#3569).

The Claude counterpart to :class:`CodexReviewScanner`: on a codex-less box the
loop still cold-reviews the user's own open PRs by dispatching ``t3:reviewer``.
The scanner emits one ``self_pr_review.dispatch`` signal per open non-draft
self-authored PR, UNCONDITIONALLY every tick — the per-SHA idempotency lives
downstream at persist time (``persistence._handle_reviewer``'s self-PR branch),
mirroring the codex scanner so a dropped persist re-fires and a force-push
re-reviews.
"""

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.codex_review import PrSummary, is_adversarial_review
from teatree.loop.scanners.self_pr_review import (
    CLAUDE_ADVERSARIAL_REVIEW_VARIANT,
    CLAUDE_STANDARD_REVIEW_VARIANT,
    ClaudeSelfPrReviewScanner,
)


@pytest.fixture(autouse=True)
def _repo_internal_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Neutralise the #1773 untrusted-public-author routing so the path-footprint
    # tests exercise diff-based classification alone (mirrors the codex test).
    monkeypatch.setattr("teatree.core.review.author_trust.repo_is_internal", lambda *a, **k: True)


SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
NEW_HEAD = "1234567890abcdeffeedfacecafebabe87654321"


def _pr(
    *,
    pr_id: int = 3569,
    head: str = HEAD,
    is_draft: bool = False,
    changed_files: tuple[str, ...] = ("src/teatree/loop/scanners/self_pr_review.py",),
    author: str = "souliane",
) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=pr_id,
        head_sha=head,
        is_draft=is_draft,
        changed_files=changed_files,
        url=f"https://github.com/{SLUG}/pull/{pr_id}",
        title=f"PR {pr_id}",
        author=author,
    )


@dataclass(slots=True)
class FakeSelfPrApi:
    """Mock ``CodexPrApi`` — captures list-PR calls and returns canned results."""

    prs_by_slug: dict[str, list[PrSummary]] = field(default_factory=dict)
    list_calls: list[str] = field(default_factory=list)

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        self.list_calls.append(slug)
        return list(self.prs_by_slug.get(slug, ()))


def _scanner(
    *,
    api: FakeSelfPrApi,
    repos: tuple[str, ...] = (SLUG,),
    overlay: str = "teatree",
) -> ClaudeSelfPrReviewScanner:
    return ClaudeSelfPrReviewScanner(repos=repos, api=api, overlay=overlay)


class TestDispatch:
    def test_open_self_pr_emits_self_pr_review_dispatch(self) -> None:
        api = FakeSelfPrApi(prs_by_slug={SLUG: [_pr()]})
        signals = _scanner(api=api).scan()

        assert [s.kind for s in signals] == ["self_pr_review.dispatch"]
        payload = signals[0].payload
        assert payload["slug"] == SLUG
        assert payload["pr_id"] == 3569
        assert payload["head_sha"] == HEAD
        assert payload["pr_url"] == f"https://github.com/{SLUG}/pull/3569"
        # ``url`` mirrors ``pr_url`` so the reviewer handler resolves the ticket URL.
        assert payload["url"] == payload["pr_url"]
        assert payload["variant"] == CLAUDE_STANDARD_REVIEW_VARIANT
        assert payload["self_pr"] is True
        assert payload["overlay"] == "teatree"

    def test_scanner_emits_unconditionally_every_tick(self) -> None:
        # Dedup is at persist time (mirrors codex); the scanner itself never goes
        # silent on a re-tick — a dropped persist must be retryable.
        api = FakeSelfPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        first = scanner.scan()
        second = scanner.scan()

        assert [s.kind for s in first] == ["self_pr_review.dispatch"]
        assert [s.kind for s in second] == ["self_pr_review.dispatch"]

    def test_adversarial_path_routes_to_hardened_variant(self) -> None:
        api = FakeSelfPrApi(prs_by_slug={SLUG: [_pr(changed_files=("src/app/auth/perms.py",))]})
        signals = _scanner(api=api).scan()

        assert signals[0].payload["variant"] == CLAUDE_ADVERSARIAL_REVIEW_VARIANT


class TestAdversarialClassifier:
    """The shared ``is_adversarial_review`` predicate the self-PR scanner routes on."""

    def test_high_stakes_path_is_adversarial(self) -> None:
        assert is_adversarial_review(("src/app/migrations/0002.py",)) is True

    def test_ordinary_path_is_not_adversarial(self) -> None:
        assert is_adversarial_review(("src/app/views.py",)) is False


class TestSkipPaths:
    def test_draft_pr_is_skipped(self) -> None:
        api = FakeSelfPrApi(prs_by_slug={SLUG: [_pr(is_draft=True)]})
        assert _scanner(api=api).scan() == []

    def test_no_repos_no_signals_no_api_calls(self) -> None:
        api = FakeSelfPrApi()
        scanner = _scanner(api=api, repos=())

        assert scanner.scan() == []
        assert api.list_calls == []


@dataclass(slots=True)
class _RaisingApi:
    """A ``CodexPrApi`` whose list raises the injected exception."""

    exc: Exception

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        raise self.exc


class TestFaultIsolation:
    def test_scanner_error_propagates_to_dispatcher(self) -> None:
        # A recoverable upstream error (auth/rate-limit) must PROPAGATE so the tick
        # report records it — never silently collapse to an empty list.
        api = _RaisingApi(ScannerError(scanner="self_pr_review", error_class=ScannerErrorClass.AUTH))
        with pytest.raises(ScannerError):
            _scanner(api=api).scan()

    def test_unexpected_list_exception_is_isolated_to_empty(self) -> None:
        api = _RaisingApi(RuntimeError("boom"))
        assert _scanner(api=api).scan() == []

    def test_one_bad_pr_does_not_drop_the_rest(self) -> None:
        # A PR whose changed_files is not iterable trips _evaluate; the other PR
        # still dispatches (per-PR fault isolation).
        bad = MagicMock()
        bad.slug, bad.number, bad.head_sha, bad.is_draft = SLUG, 2, HEAD, False
        bad.author, bad.changed_files, bad.url, bad.title = "souliane", None, "", ""
        api = FakeSelfPrApi(prs_by_slug={SLUG: [bad, _pr(pr_id=1)]})

        dispatched = sorted(s.payload["pr_id"] for s in _scanner(api=api).scan())

        assert dispatched == [1]
