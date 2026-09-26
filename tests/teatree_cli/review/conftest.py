"""Multi-overlay fixtures for the review-CLI tests.

Both reads a review post depends on — the GitLab credential and the API base URL
— resolve through an overlay, and a process with several overlays registered has
no ambient answer: ``get_overlay()`` raises ``Multiple overlays found``. These
fixtures reproduce exactly that install so the tests can prove the reads resolve
from the overlay OWNING the target repo (souliane/teatree#1814 class).

Overlays are registered through ``_discover_overlays`` — the live registry both
``get_overlay`` and ``get_all_overlays`` route through — so nothing about overlay
resolution itself is stubbed. Only the third-party ``glab`` binary is.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from unittest import mock

import httpx
import pytest

from teatree.cli.review import inline_shape_gate, shape_gate
from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase, OverlayConfig, ProvisionStep
from teatree.utils import run as utils_run_mod


class InlineTokenConfig(OverlayConfig):
    """Overlay config carrying its GitLab credential inline instead of via ``pass``."""

    inline_token: str = ""

    def get_gitlab_token(self) -> str:
        return self.inline_token


class OwnedRepoOverlay(OverlayBase):
    """A registered overlay owning one repo slug, with its own credential and instance URL."""

    def __init__(self, repo: str, *, token: str, url: str) -> None:
        super().__init__()
        self._repo = repo
        self.config = InlineTokenConfig(inline_token=token, gitlab_url=url)

    def get_repos(self) -> list[str]:
        return [self._repo]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        del worktree
        return []


@pytest.fixture
def two_overlays(monkeypatch: pytest.MonkeyPatch, _clear_backend_caches: None) -> Iterator[dict[str, OwnedRepoOverlay]]:
    """Register two overlays with no ambient winner — the shape that breaks ``get_overlay()``.

    Yields the registered overlays so a test can assert against the very config it
    registered instead of a duplicated literal.

    ``alpha`` owns ``acme/alpha`` and holds a credential; ``bravo`` owns
    ``acme/bravo`` and holds none, so the "genuinely absent credential" case is
    reachable without breaking the read. ``acme/unclaimed`` is owned by neither,
    which is what makes the ambient lookup — and therefore the read — fail.

    The suite pins ``T3_OVERLAY_NAME=t3-teatree`` for determinism and ``get_overlay``
    also falls back to the CWD's overlay, so both tiers are removed: the env pin is
    dropped and the CWD is moved out of any registered overlay's tree.

    The registry patch is scoped with ``mock.patch`` (not ``monkeypatch``) and
    ``_clear_backend_caches`` is requested explicitly, so the real ``lru_cache``-wrapped
    ``_discover_overlays`` is restored BEFORE that autouse fixture's exit calls
    ``reset_overlay_cache()`` — which needs the ``cache_clear`` attribute back.
    """
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.chdir(os.sep)
    overlays = {
        "alpha": OwnedRepoOverlay("acme/alpha", token="glpat-ALPHA", url="https://alpha.example.com/api/v4"),
        "bravo": OwnedRepoOverlay("acme/bravo", token="", url="https://bravo.example.com/api/v4"),
    }
    with mock.patch("teatree.core.overlay_loader._discover_overlays", return_value=overlays):
        yield overlays


@pytest.fixture
def no_glab_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the third-party ``glab`` binary to "not authenticated"."""
    monkeypatch.setattr(
        utils_run_mod.subprocess,
        "run",
        lambda *_a, **_kw: mock.MagicMock(stderr="", stdout="", returncode=1),
    )


@dataclass
class OutboundHttpBan:
    """Counts the outbound requests the ban intercepted, for tests that assert zero."""

    attempts: int = 0


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch: pytest.MonkeyPatch) -> OutboundHttpBan:
    """Refuse every real HTTP request this package's tests would make.

    The forge POST is stubbed in these tests, but the pre-publish gate chain and the
    author read in front of it are not: `ReviewService.post_comment` reached
    `gitlab.com/api/v4` six times in a clean run of one file, 401ing, and the calls
    passed only because `guarded_read` degrades a failed read to its neutral. A gate
    exercised against a 401 proves nothing about the gate, and the requests time out
    under xdist.

    Broad on purpose: it patches the transport, so it catches a read at ANY seam rather
    than the seams a test remembered to stub. `guarded_read` swallows the raise, which
    is why the counter exists -- a test asserting `attempts == 0` sees a read the
    exception alone would have hidden.
    """
    ban = OutboundHttpBan()

    def _refuse(_self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        ban.attempts += 1
        msg = f"outbound HTTP is banned in this package: {request.method} {request.url}"
        raise AssertionError(msg)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse)
    return ban


@pytest.fixture
def forge_reads_stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the two pre-publish gate reads locally, at their own seams.

    `check_review_shape` reads the MR author and `check_inline_shape` counts the
    existing inline drafts. Both degrade through `guarded_read`, so today they reach
    gitlab.com, 401, and the gate runs on the neutral -- which proves nothing about the
    gate and times out under xdist. The stubs return those same neutrals, an unreadable
    author and no pending drafts, so the branch each gate takes is chosen here rather
    than inherited from a failed request.
    """
    monkeypatch.setattr(shape_gate, "fetch_mr_author", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(inline_shape_gate, "count_inline_drafts", lambda *_args, **_kwargs: 0)
