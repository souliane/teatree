"""Review-target selection skips merged/closed MRs at the dispatch chokepoint (#2081).

GitLab is the source of truth: a review request for an MR that is already
MERGED or CLOSED can never have a note land on it, so the loop must not
dispatch a ``t3:reviewer`` for it. The single chokepoint every
mention/DM/task/review-intent review-request flows through is
``_review_request_dispatch`` + ``_gate_review_intent``.

Fail-open doctrine (``get_pr_open_state`` returns UNKNOWN on any API hiccup):
the gate suppresses ONLY on a definite MERGED/CLOSED — OPEN and UNKNOWN still
dispatch, so a transient lookup failure never silently drops a legitimate review.
"""

import os
from unittest.mock import patch

import pytest

from teatree.core.backend_protocols import PrOpenState
from teatree.core.overlay import OverlayBase
from teatree.loop import dispatch as dispatch_mod
from teatree.loop.scanners.base import ScanSignal

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/42"


class _StubHost:
    def __init__(self, state: PrOpenState) -> None:
        self.state = state
        self.queried: list[str] = []

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        self.queried.append(pr_url)
        return self.state


@pytest.fixture(autouse=True)
def _review_loop_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #79 review-loop gate must be ON so the only suppression under test is
    # the live-state skip.
    monkeypatch.setattr("teatree.loop.review_claim.review_loop_enabled", lambda: True)


def _bind_host(monkeypatch: pytest.MonkeyPatch, host: _StubHost | None) -> None:
    monkeypatch.setattr(
        "teatree.backends.loader.get_code_host_for_url",
        lambda *_args, **_kwargs: host,
    )
    monkeypatch.setattr(
        "teatree.core.overlay_loader.get_overlay",
        lambda *_args, **_kwargs: object(),
    )


def _review_intent_signal() -> ScanSignal:
    return ScanSignal(
        kind="slack.review_intent",
        summary=f"Review intent: {_MR_URL}",
        payload={"url": _MR_URL, "mr_url": _MR_URL, "overlay": ""},
    )


def _mention_signal() -> ScanSignal:
    return ScanSignal(
        kind="slack.mention",
        summary="review please",
        payload={"event": {"text": f"can you review {_MR_URL}"}, "overlay": ""},
    )


class TestReviewIntentSkipsMergedClosed:
    def test_merged_suppresses_reviewer_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = _StubHost(PrOpenState.MERGED)
        _bind_host(monkeypatch, host)
        actions = dispatch_mod.dispatch([_review_intent_signal()])
        assert not any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)
        assert host.queried == [_MR_URL]

    def test_closed_suppresses_reviewer_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_host(monkeypatch, _StubHost(PrOpenState.CLOSED))
        actions = dispatch_mod.dispatch([_review_intent_signal()])
        assert not any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)

    def test_open_still_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_host(monkeypatch, _StubHost(PrOpenState.OPEN))
        actions = dispatch_mod.dispatch([_review_intent_signal()])
        assert any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)

    def test_unknown_fails_open_and_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_host(monkeypatch, _StubHost(PrOpenState.UNKNOWN))
        actions = dispatch_mod.dispatch([_review_intent_signal()])
        assert any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)

    def test_no_host_fails_open_and_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_host(monkeypatch, None)
        actions = dispatch_mod.dispatch([_review_intent_signal()])
        assert any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)

    def test_host_resolution_exception_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: object, **_kwargs: object) -> object:
            msg = "overlay resolution blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr("teatree.core.overlay_loader.get_overlay", _boom)
        actions = dispatch_mod.dispatch([_review_intent_signal()])
        assert any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)


class TestEmptyUrlNeverSuppresses:
    def test_review_target_is_dead_returns_false_on_empty_url(self) -> None:
        # No URL to check (review-intent without a parsable MR url) must never
        # suppress — there is nothing to confirm dead.
        assert dispatch_mod._review_target_is_dead("") is False


class TestMentionReviewRequestSkipsMergedClosed:
    def test_merged_mention_suppresses_reviewer_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_host(monkeypatch, _StubHost(PrOpenState.MERGED))
        actions = dispatch_mod.dispatch([_mention_signal()])
        assert not any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)

    def test_open_mention_still_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_host(monkeypatch, _StubHost(PrOpenState.OPEN))
        actions = dispatch_mod.dispatch([_mention_signal()])
        assert any(a.kind == "agent" and a.zone == "t3:reviewer" for a in actions)


class _RepoOverlay(OverlayBase):
    def __init__(self, repos: list[str]) -> None:
        self._repos = repos

    def get_repos(self) -> list[str]:
        return self._repos

    def get_workspace_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree: object) -> list:
        _ = worktree
        return []


class TestReviewTargetMultiOverlay:
    """Real ``get_overlay()`` ambiguity path — two overlays registered (TODO-282).

    ``_review_target_is_dead`` resolved the overlay with a bare
    ``get_overlay()`` to build the per-URL code host. With two overlays
    registered and no ``T3_OVERLAY_NAME`` that raises ``Multiple overlays
    found`` — caught by the function's own ``except Exception`` (fail-open),
    so a genuinely MERGED/CLOSED MR is NEVER recognised as dead and a stale
    reviewer is dispatched on every multi-overlay host. The fix resolves the
    overlay from the MR URL, so the live MERGED state is read and the dead
    target is correctly suppressed.

    Only overlay discovery and the network host are stubbed; the URL→overlay
    resolution is real.
    """

    def test_merged_target_recognised_dead_with_two_overlays(self) -> None:
        url = "https://gitlab.com/acme/backend/-/merge_requests/55"
        overlays = {
            "acme": _RepoOverlay(["acme/backend"]),
            "other": _RepoOverlay(["other/repo"]),
        }
        env_without_pin = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
        with (
            patch.dict(os.environ, env_without_pin, clear=True),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays),
            patch(
                "teatree.backends.loader.get_code_host_for_url",
                return_value=_StubHost(PrOpenState.MERGED),
            ),
        ):
            result = dispatch_mod._review_target_is_dead(url)

        assert result is True, (
            "with two overlays registered, a MERGED MR must still be recognised dead — "
            "a bare get_overlay() raises Multiple-overlays and the function fails open to False"
        )
