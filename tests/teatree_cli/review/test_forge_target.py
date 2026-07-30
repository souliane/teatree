"""The review CLI derives its forge target from the repo it was given (#3793/#3794).

``t3 review <cmd> <repo> <mr> …`` names its target in the argument list, so the
overlay that owns that repo — and therefore the base URL and API token every post
is addressed with — is derivable from the invocation itself. Resolving them from
the AMBIENT overlay instead makes the whole surface conditional on how many
overlays happen to be registered: with more than one and no explicit pin,
``get_overlay()`` raises ``Multiple overlays found`` and every post dies (#3793).

The two reads are one decision, so they resolve through one overlay: a base URL
from overlay B addressed with a token resolved independently of B is a
cross-instance mismatch, not a working post.

A failed read stays distinguishable from a genuinely absent one, so the CLI can
name the real cause instead of the login hint that changes nothing (#3794).
"""

import re
from functools import lru_cache
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import forge_target as forge_target_mod
from teatree.cli.review.guarded_read import ReadRefusedError
from teatree.cli.review.mcp_seam import _build_review_service
from teatree.cli.review.service import ReviewService
from teatree.core.overlay import OverlayBase, OverlayConfig

runner = CliRunner()

_OWNED = "acme/widgets"
_OWNER_URL = "https://gitlab.acme.example/api/v4"
_OWNER_TOKEN = "token-owned-by-acme"
_OTHER_URL = "https://gitlab.other.example/api/v4"
_OTHER_TOKEN = "token-owned-by-other"


class _ForgeConfig(OverlayConfig):
    """An overlay config whose GitLab token is a plain declared field."""

    forge_token: str = ""

    def get_gitlab_token(self) -> str:
        return self.forge_token


class _ForgeOverlay(OverlayBase):
    """A registered overlay owning fixed repo slugs, with its own forge coordinates."""

    def __init__(self, repos: list[str], *, gitlab_url: str, token: str) -> None:
        super().__init__()
        self._repos = repos
        self.config = _ForgeConfig(gitlab_url=gitlab_url, forge_token=token)

    def get_repos(self) -> list[str]:
        return self._repos

    def get_provision_steps(self, worktree):
        return []


def _two_overlays() -> dict[str, OverlayBase]:
    """A two-overlay install where exactly one overlay owns ``acme/widgets``."""
    return {
        "acme-overlay": _ForgeOverlay([_OWNED], gitlab_url=_OWNER_URL, token=_OWNER_TOKEN),
        "other-overlay": _ForgeOverlay(["other/repo"], gitlab_url=_OTHER_URL, token=_OTHER_TOKEN),
    }


def _register(monkeypatch: pytest.MonkeyPatch, overlays: dict[str, OverlayBase]) -> None:
    """Stand in for the cached discovery, keeping the ``cache_clear`` the fixtures reset."""
    monkeypatch.setattr("teatree.core.overlay_loader._discover_overlays", lru_cache(maxsize=1)(lambda: overlays))


@pytest.fixture
def multi_overlay_install(monkeypatch: pytest.MonkeyPatch) -> dict[str, OverlayBase]:
    """Register two overlays with no ambient pin and no forge env overrides.

    The conftest pins ``T3_OVERLAY_NAME`` globally, which would short-circuit the
    ambiguity this suite is about; ``$GITLAB_URL`` / ``$GITLAB_TOKEN`` are cleared
    so the resolution under test is the overlay one, not an operator override.
    """
    for name in ("T3_OVERLAY_NAME", "GITLAB_URL", "GITLAB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    overlays = _two_overlays()
    _register(monkeypatch, overlays)
    return overlays


class TestBaseUrlComesFromTheOwningOverlay:
    def test_multi_overlay_install_resolves_the_repo_owner(self, multi_overlay_install) -> None:
        """The whole point of #3793: two overlays registered, and the post still lands."""
        assert ReviewService(token="t", repo=_OWNED)._resolve_base_url() == _OWNER_URL

    def test_the_non_owning_overlay_is_never_addressed(self, multi_overlay_install) -> None:
        assert ReviewService(token="t", repo="other/repo")._resolve_base_url() == _OTHER_URL

    def test_a_repo_no_overlay_owns_still_refuses_rather_than_guessing(self, multi_overlay_install) -> None:
        """Unowned + ambiguous is the case with no safe answer — refuse, never pick one."""
        with pytest.raises(ReadRefusedError):
            ReviewService(token="t", repo="nobody/knows")._resolve_base_url()

    def test_an_explicit_env_url_is_still_the_operator_override(
        self, multi_overlay_install, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_URL", "https://gitlab.pinned.example/api/v4")
        # The owning overlay still wins when it is readable — env is the fallback.
        assert ReviewService(token="t", repo=_OWNED)._resolve_base_url() == _OWNER_URL
        assert (
            ReviewService(token="t", repo="nobody/knows")._resolve_base_url() == "https://gitlab.pinned.example/api/v4"
        )


class TestTokenComesFromTheOwningOverlay:
    def test_multi_overlay_install_resolves_the_repo_owner(self, multi_overlay_install) -> None:
        assert ReviewService.get_gitlab_token(_OWNED) == _OWNER_TOKEN

    def test_the_non_owning_overlays_token_is_never_used(self, multi_overlay_install) -> None:
        assert ReviewService.get_gitlab_token("other/repo") == _OTHER_TOKEN

    def test_a_failed_read_is_reported_as_failed_not_as_an_empty_token(self, multi_overlay_install) -> None:
        """#3794: ``failed`` must survive the read so the caller can name the real cause."""
        outcome = ReviewService.read_gitlab_token("nobody/knows")
        assert outcome.failed
        assert outcome.value == ""

    def test_a_genuinely_absent_token_is_not_a_failed_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        overlays = {"acme-overlay": _ForgeOverlay([_OWNED], gitlab_url=_OWNER_URL, token="")}
        _register(monkeypatch, overlays)
        monkeypatch.setattr("teatree.cli.review.forge_target._glab_login_token", lambda: "")
        outcome = ReviewService.read_gitlab_token(_OWNED)
        assert not outcome.failed
        assert outcome.value == ""


class TestRequireTokenNamesTheRealCause:
    """#3794: an unresolvable read must not be reported as a missing ``glab`` login."""

    def _invoke(self, args: list[str]):
        return runner.invoke(app, args)

    def test_a_failed_read_does_not_send_the_user_to_glab_auth_login(self, multi_overlay_install) -> None:
        result = self._invoke(["review", "post-comment", "nobody/knows", "1", "note", "--file", "a.py", "--line", "1"])
        assert result.exit_code == 1
        assert "glab auth login" not in result.output
        assert "nobody/knows" in result.output

    def test_a_genuinely_absent_token_keeps_the_login_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        overlays = {"acme-overlay": _ForgeOverlay([_OWNED], gitlab_url=_OWNER_URL, token="")}
        _register(monkeypatch, overlays)
        monkeypatch.setattr("teatree.cli.review.forge_target._glab_login_token", lambda: "")
        result = self._invoke(["review", "post-comment", _OWNED, "1", "note", "--file", "a.py", "--line", "1"])
        assert result.exit_code == 1
        assert "glab auth login" in result.output


class TestAmbientResolutionIsGone:
    """The forge-target reads no longer reach the ambient ``get_overlay()``.

    A behavioural assertion cannot see the difference between "resolved from the
    URL" and "resolved ambiently and happened to be right", so this one reads the
    module: the ambient entry point must be absent from the resolution path, not
    merely shadowed by a happier default.
    """

    def test_the_resolver_module_never_reaches_the_ambient_overlay(self) -> None:
        source = Path(forge_target_mod.__file__).read_text(encoding="utf-8")
        assert "get_overlay_for_url" in source
        assert not re.search(r"\bimport get_overlay\b", source)


class TestMcpSeamCarriesTheRepo:
    """The MCP review seam builds its service for the repo the tool was called with."""

    def test_the_seam_factory_takes_the_target_repo(self, multi_overlay_install) -> None:
        service = _build_review_service(_OWNED)
        assert service.repo == _OWNED
        assert service.token == _OWNER_TOKEN
