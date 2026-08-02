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
from unittest import mock

import pytest

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
