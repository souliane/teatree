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
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import forge_target as forge_target_mod
from teatree.cli.review.guarded_read import ReadRefusedError
from teatree.cli.review.mcp_seam import _build_review_poster
from teatree.cli.review.service import ReviewService
from teatree.config.credential_pass_key import PassKeyResolution, PassKeySource
from teatree.core.authoring_credential import gitlab_token_for_remote, reset_authoring_credential_cache
from teatree.core.overlay import OverlayBase, OverlayConfig
from teatree.core.overlay_loader import OverlayConfigResolver
from teatree.utils.git_remote import slug_from_remote

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
        assert ReviewService(token="t", repo=_OWNED)._resolve_base_url() == "https://gitlab.pinned.example/api/v4"
        assert (
            ReviewService(token="t", repo="nobody/knows")._resolve_base_url() == "https://gitlab.pinned.example/api/v4"
        )


class TestTokenComesFromTheOwningOverlay:
    def test_explicit_environment_token_wins_before_the_owner_route(
        self, multi_overlay_install, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "explicit-operator-token")

        assert ReviewService.get_gitlab_token(_OWNED) == "explicit-operator-token"

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
        outcome = ReviewService.read_gitlab_token(_OWNED)
        assert not outcome.failed
        assert outcome.value == ""

    def test_absent_owner_route_never_inherits_the_logged_in_glab_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        overlays = {"acme-overlay": _ForgeOverlay([_OWNED], gitlab_url=_OWNER_URL, token="")}
        _register(monkeypatch, overlays)
        outcome = ReviewService.read_gitlab_token(_OWNED)

        assert outcome == forge_target_mod.ReadOutcome(value="", failed=False)

    def test_unreadable_owner_route_stays_failed_and_never_inherits_ambient_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        overlays = {"acme-overlay": _ForgeOverlay([_OWNED], gitlab_url=_OWNER_URL, token="")}
        _register(monkeypatch, overlays)
        monkeypatch.setattr(
            _ForgeConfig,
            "resolve_pass_key",
            lambda _self, _credential: PassKeyResolution("gitlab_token_pass_key", "", PassKeySource.UNREADABLE),
        )
        outcome = ReviewService.read_gitlab_token(_OWNED)

        assert outcome.failed
        assert outcome.value == ""
        assert "gitlab_token_pass_key" in str(outcome.error)


class TestApprovalUsesTheOwnerNotTheAuthoringCredential:
    """The review/approve surface reads the OWNER credential, never the authoring one.

    An overlay that authors one repo's MRs under a bot does so for one reason: the forge
    bars an MR's AUTHOR from approving it, so a distinct author is what keeps the owner's
    account eligible. Routing this surface through ``get_gitlab_token_for_remote`` would
    therefore record every review and approval under the bot — the single identity that
    must never carry one — and leave the repo with no eligible approver at all.
    """

    _AUTHORING_TOKEN = "token-of-the-authoring-bot"

    @pytest.fixture
    def bot_authored_install(self, monkeypatch: pytest.MonkeyPatch) -> OverlayBase:
        for name in ("T3_OVERLAY_NAME", "GITLAB_URL", "GITLAB_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        overlay = _ForgeOverlay([_OWNED], gitlab_url=_OWNER_URL, token=_OWNER_TOKEN)
        overlay.config.get_gitlab_token_for_remote = (  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
            lambda remote: self._AUTHORING_TOKEN if _OWNED in remote else _OWNER_TOKEN
        )
        _register(monkeypatch, {"acme-overlay": overlay})
        return overlay

    def test_the_review_token_is_the_owner_credential(self, bot_authored_install: OverlayBase) -> None:
        assert ReviewService.get_gitlab_token(_OWNED) == _OWNER_TOKEN

    def test_the_authoring_credential_never_reaches_the_review_surface(self, bot_authored_install: OverlayBase) -> None:
        assert ReviewService.get_gitlab_token(_OWNED) != self._AUTHORING_TOKEN

    def test_control_the_overlay_really_does_author_under_a_distinct_credential(
        self, bot_authored_install: OverlayBase
    ) -> None:
        # Without this the two assertions above pass on an overlay carrying no override
        # at all, which would make the whole class vacuous.
        config = bot_authored_install.config
        assert config.get_gitlab_token_for_remote(f"git@gitlab.acme.example:{_OWNED}.git") == self._AUTHORING_TOKEN
        assert config.get_gitlab_token() == _OWNER_TOKEN


class TestRequireTokenNamesTheRealCause:
    """#3794: an unresolvable read must not be reported as a missing ``glab`` login."""

    def _invoke(self, args: list[str]):
        return runner.invoke(app, args)

    def test_a_failed_read_does_not_send_the_user_to_glab_auth_login(self, multi_overlay_install) -> None:
        result = self._invoke(["review", "post-comment", "nobody/knows", "1", "note", "--file", "a.py", "--line", "1"])
        assert result.exit_code == 1
        assert "glab auth login" not in result.output
        assert "nobody/knows" in result.output

    def test_a_genuinely_absent_token_refuses_without_suggesting_ambient_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        overlays = {"acme-overlay": _ForgeOverlay([_OWNED], gitlab_url=_OWNER_URL, token="")}
        _register(monkeypatch, overlays)
        result = self._invoke(["review", "post-comment", _OWNED, "1", "note", "--file", "a.py", "--line", "1"])
        assert result.exit_code == 1
        assert "glab auth login" not in result.output
        assert "gitlab_token_pass_key" in result.output


class TestAmbientResolutionIsGone:
    """The forge-target reads no longer reach the ambient ``get_overlay()``.

    A behavioural assertion cannot see the difference between "resolved from the
    repo" and "resolved ambiently and happened to be right", so this one reads the
    module: the ambient entry point must be absent from the resolution path, not
    merely shadowed by a happier default.
    """

    def test_the_resolver_module_never_reaches_the_ambient_overlay(self) -> None:
        source = Path(forge_target_mod.__file__).read_text(encoding="utf-8")
        assert re.findall(r"^[ \t]*return get_overlay\(.*\)$", source, re.MULTILINE) == [
            "    return get_overlay(owning_overlay_name(repo) or None)"
        ]

    def test_the_resolver_never_reads_the_logged_in_glab_account(self) -> None:
        source = Path(forge_target_mod.__file__).read_text(encoding="utf-8")
        assert "glab auth status" not in source


class TestTheDeclaredNamespaceIsTheLastTier:
    """A repo no overlay's table lists still resolves to the overlay that declares its group."""

    def test_a_repo_outside_every_table_resolves_to_its_declared_group_owner(self, multi_overlay_install) -> None:
        multi_overlay_install["acme-overlay"].config.owned_repos = {"gitlab.com": ["acme"]}

        assert forge_target_mod.owning_overlay_name("acme/never-enumerated") == "acme-overlay"

    def test_an_enumerated_repo_still_wins_over_a_group_claim(self, multi_overlay_install) -> None:
        multi_overlay_install["other-overlay"].config.owned_repos = {"gitlab.com": ["acme"]}

        assert forge_target_mod.owning_overlay_name(_OWNED) == "acme-overlay"

    def test_a_group_declared_on_another_forge_never_attributes_a_gitlab_post(self, multi_overlay_install) -> None:
        multi_overlay_install["acme-overlay"].config.owned_repos = {"github.com": ["acme"]}

        assert forge_target_mod.owning_overlay_name("acme/never-enumerated") == ""

    def test_a_group_two_overlays_claim_resolves_to_neither(self, multi_overlay_install) -> None:
        for overlay in multi_overlay_install.values():
            overlay.config.owned_repos = {"gitlab.com": ["acme"]}

        assert forge_target_mod.owning_overlay_name("acme/never-enumerated") == ""

    def test_an_overlay_whose_scope_will_not_read_declines_the_attribution_but_is_not_fatal(
        self,
        multi_overlay_install,
    ) -> None:
        """An unreadable scope could be the second half of a tie, so nobody is named.

        Skipping it instead would collapse that tie into one owner — and that
        owner's token and base URL would then address the post.
        """
        multi_overlay_install["acme-overlay"].config.owned_repos = {"gitlab.com": ["acme"]}
        real = OverlayConfigResolver.owned_repos

        def read(name: str | None) -> dict[str, list[str]]:
            if name == "other-overlay":
                msg = "overlay config unreadable"
                raise RuntimeError(msg)
            return real(name)

        with mock.patch.object(OverlayConfigResolver, "owned_repos", side_effect=read):
            assert forge_target_mod.owning_overlay_name("acme/never-enumerated") == ""


class TestMcpSeamCarriesTheRepo:
    """The MCP review seam builds its service for the repo the tool was called with."""

    def test_the_seam_factory_takes_the_target_repo(self, multi_overlay_install) -> None:
        service = _build_review_poster(_OWNED).service
        assert service.repo == _OWNED
        assert service.token == _OWNER_TOKEN


class TestAScopedRepoIsAuthoredAsItsIdentityAndApprovedAsTheOwner:
    """A repo scoped to a distinct author is written under it and approved under the owner.

    Authoring and pushing present the per-remote credential, so the owner never authors
    the MR; reviewing and approving present the owner's, because the forge bars an MR's
    author from approving it. Every other repo answers the overlay-wide token on both paths.
    """

    _SCOPED = "acme/bot-authored"
    _SCOPED_REMOTE = "git@gitlab.acme.example:acme/bot-authored.git"
    _OWNED_REMOTE = f"git@gitlab.acme.example:{_OWNED}.git"
    _SCOPED_TOKEN = "token-of-the-bot-that-authors-acme-bot-authored"

    class _ScopedConfig(_ForgeConfig):
        """A config that authors exactly one repo under a distinct identity."""

        def get_gitlab_token_for_remote(self, remote: str) -> str:
            scoped = TestAScopedRepoIsAuthoredAsItsIdentityAndApprovedAsTheOwner
            return scoped._SCOPED_TOKEN if slug_from_remote(remote) == scoped._SCOPED else self.get_gitlab_token()

    @pytest.fixture
    def scoped_config(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[OverlayConfig]:
        for name in ("T3_OVERLAY_NAME", "GITLAB_URL", "GITLAB_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        overlay = _ForgeOverlay([_OWNED, self._SCOPED], gitlab_url=_OWNER_URL, token=_OWNER_TOKEN)
        overlay.config = self._ScopedConfig(gitlab_url=_OWNER_URL, forge_token=_OWNER_TOKEN)
        _register(monkeypatch, {"acme-overlay": overlay})
        reset_authoring_credential_cache()
        yield overlay.config
        reset_authoring_credential_cache()

    def test_approving_the_scoped_repo_presents_the_owner_credential(self, scoped_config: OverlayConfig) -> None:
        assert ReviewService.get_gitlab_token(self._SCOPED) == _OWNER_TOKEN

    def test_authoring_the_scoped_repo_presents_its_own_credential(self, scoped_config: OverlayConfig) -> None:
        assert gitlab_token_for_remote(scoped_config, self._SCOPED_REMOTE) == self._SCOPED_TOKEN

    def test_every_other_repo_answers_the_overlay_wide_credential_on_both_paths(
        self, scoped_config: OverlayConfig
    ) -> None:
        assert ReviewService.get_gitlab_token(_OWNED) == _OWNER_TOKEN
        assert gitlab_token_for_remote(scoped_config, self._OWNED_REMOTE) == _OWNER_TOKEN
