"""Tests for :class:`CodexReviewScanner` — auto-dispatch ``/codex:review`` (#1254).

The scanner is the structural fix for the "user has to remember to run
``/codex:review`` after every push" failure mode encoded in the
``feedback_fleet_of_agents_with_codex_doublecheck`` binding. It runs
every tick, walks the configured repo list, and emits one
``codex_review.dispatch`` signal per open self-authored PR whose head SHA
the scanner hasn't seen before — keyed on ``(slug, pr_id, head_sha)``
via :class:`CodexReviewMarker`. Re-ticking on the same SHA is a no-op;
a force-push (new SHA) re-fires.

The scanner is the loop-level enforcement of the fleet-of-agents rule:
the user never has to ask "have you run codex on this?" again.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.loop.scanners.codex_review import CodexReviewScanner, PrSummary

pytestmark = pytest.mark.django_db


SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
NEW_HEAD = "1234567890abcdeffeedfacecafebabe87654321"
AUTH_HEAD = "abc1230000000000000000000000000000000000"


def _pr(
    *,
    pr_id: int = 1254,
    head: str = HEAD,
    is_draft: bool = False,
    changed_files: tuple[str, ...] = ("src/teatree/loop/scanners/codex_review.py",),
) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=pr_id,
        head_sha=head,
        is_draft=is_draft,
        changed_files=changed_files,
        url=f"https://github.com/{SLUG}/pull/{pr_id}",
        title=f"PR {pr_id}",
    )


@dataclass(slots=True)
class FakeCodexPrApi:
    """Mock ``CodexPrApi`` — captures list-PR calls and returns canned results."""

    prs_by_slug: dict[str, list[PrSummary]] = field(default_factory=dict)
    list_calls: list[str] = field(default_factory=list)

    def list_open_self_prs(self, *, slug: str) -> list[PrSummary]:
        self.list_calls.append(slug)
        return list(self.prs_by_slug.get(slug, ()))


def _scanner(
    *,
    api: FakeCodexPrApi,
    repos: tuple[str, ...] = (SLUG,),
    overlay: str = "teatree",
) -> CodexReviewScanner:
    return CodexReviewScanner(repos=repos, api=api, overlay=overlay)


class TestDispatchOnNewSha:
    def test_new_pr_emits_codex_review_dispatch_signal(self) -> None:
        """An unseen ``(slug, pr_id, head_sha)`` triggers one dispatch signal."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["codex_review.dispatch"]
        payload = signals[0].payload
        assert payload["slug"] == SLUG
        assert payload["pr_id"] == 1254
        assert payload["head_sha"] == HEAD
        assert payload["pr_url"] == f"https://github.com/{SLUG}/pull/1254"
        assert payload["overlay"] == "teatree"

    def test_marker_row_persisted_after_dispatch(self) -> None:
        """After dispatch, a :class:`CodexReviewMarker` row makes re-ticks a no-op."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        scanner.scan()

        marker = CodexReviewMarker.objects.get(slug=SLUG, pr_id=1254, head_sha=HEAD)
        assert marker.overlay == "teatree"

    def test_second_tick_on_same_head_does_not_redispatch(self) -> None:
        """The hard requirement: re-ticking on the same SHA is silent."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        first = scanner.scan()
        second = scanner.scan()

        assert [s.kind for s in first] == ["codex_review.dispatch"]
        assert second == []

    def test_new_head_sha_after_force_push_redispatches(self) -> None:
        """A new head SHA on the same PR (force-push) fires a fresh dispatch."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        scanner.scan()
        api.prs_by_slug[SLUG] = [_pr(head=NEW_HEAD)]
        signals = scanner.scan()

        assert [s.kind for s in signals] == ["codex_review.dispatch"]
        assert signals[0].payload["head_sha"] == NEW_HEAD


class TestSkipPaths:
    def test_draft_pr_is_skipped(self) -> None:
        """Draft PRs are not auto-reviewed — the user is still iterating."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr(is_draft=True)]})
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert signals == []
        assert not CodexReviewMarker.objects.filter(slug=SLUG, pr_id=1254).exists()

    def test_no_repos_no_signals_no_api_calls(self) -> None:
        """An empty repo list keeps the scanner silent on every tick."""
        api = FakeCodexPrApi()
        scanner = _scanner(api=api, repos=())

        signals = scanner.scan()

        assert signals == []
        assert api.list_calls == []


class TestAdversarialClassifier:
    def test_security_path_routes_to_adversarial_review(self) -> None:
        """Touching ``permissions/`` selects ``codex:adversarial-review``."""
        api = FakeCodexPrApi(
            prs_by_slug={
                SLUG: [_pr(head=AUTH_HEAD, changed_files=("src/teatree/permissions/policy.py",))],
            },
        )
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert signals[0].payload["variant"] == "codex:adversarial-review"

    def test_default_diff_routes_to_standard_review(self) -> None:
        """A run-of-the-mill diff stays on ``codex:review``."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api)

        signals = scanner.scan()

        assert signals[0].payload["variant"] == "codex:review"


class TestSignalAttribution:
    def test_signal_carries_overlay_for_multi_overlay_loop(self) -> None:
        """Multi-overlay loops attribute signals back to the originating overlay."""
        api = FakeCodexPrApi(prs_by_slug={SLUG: [_pr()]})
        scanner = _scanner(api=api, overlay="my-client-overlay")

        signals = scanner.scan()

        assert signals[0].payload["overlay"] == "my-client-overlay"
