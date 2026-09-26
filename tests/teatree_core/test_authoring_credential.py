"""A repo's authoring credential is a property of the REMOTE, never of who typed the command.

An overlay declares that one repo is written under a bot credential so the human owner stays
eligible to approve its MRs. That declaration was read off whichever overlay happened to be
AMBIENT, so an entrypoint pinned to an overlay carrying no declaration opened every MR under
the owner — the one identity a forge refuses an approval from. The MRs were born unapprovable
and nothing said so until approval time, hours or days later.
"""

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.loader import get_code_host_for_repo
from teatree.core.authoring_credential import (
    AmbiguousAuthoringCredentialError,
    authoring_identity_for_remote,
    authorized_pr_host,
    declared_distinct_author,
    gitlab_token_for_remote,
    reset_authoring_credential_cache,
    unapprovable_author_refusal,
    unresolvable_author_refusal,
)
from teatree.core.identity_wiring import AuthoringIdentity, unapprovable_author_fault
from teatree.core.overlay import OverlayBase, OverlayConfig
from teatree.core.runners.base import RunnerResult
from teatree.core.runners.ship import ShipExecutor

_ALL_OVERLAYS = "teatree.core.authoring_credential.get_all_overlays"
_APPROVERS = "teatree.core.authoring_credential.approver_identities"

OWNER_TOKEN = "owner-token"
BOT_TOKEN = "bot-token"
FACTORY = "git@gitlab.com:org/group/factory.git"
PRODUCT = "git@gitlab.com:org/product.git"


class _PlainConfig(OverlayConfig):
    """An overlay that scopes nothing — every remote answers the owner's own credential."""

    def __init__(self, *, owner: str = OWNER_TOKEN) -> None:
        super().__init__()
        self._owner = owner

    def get_gitlab_token(self) -> str:
        return self._owner


class _ScopedConfig(OverlayConfig):
    """An overlay routing ONE remote to a credential that may or may not resolve."""

    def __init__(self, *, owner: str = OWNER_TOKEN, scoped: str = BOT_TOKEN, marker: str = "factory") -> None:
        super().__init__()
        self._owner = owner
        self._scoped = scoped
        self._marker = marker

    def get_gitlab_token(self) -> str:
        return self._owner

    def get_gitlab_token_for_remote(self, remote: str) -> str:
        return self._scoped if self._marker in remote else self._owner


def _overlay(config: OverlayConfig) -> OverlayBase:
    overlay = MagicMock(spec=OverlayBase)
    overlay.config = config
    return cast("OverlayBase", overlay)


def _registry(**configs: OverlayConfig) -> dict[str, OverlayBase]:
    return {name: _overlay(config) for name, config in configs.items()}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_authoring_credential_cache()


class TestDeclaredDistinctAuthor:
    """Which overlay — if any — declares a non-owner credential for a remote."""

    def test_a_remote_nobody_declares_has_no_declared_author(self) -> None:
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=_PlainConfig())):
            assert declared_distinct_author(PRODUCT) is None

    def test_a_declared_remote_names_its_overlay_and_credential(self) -> None:
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=_PlainConfig(), product=_ScopedConfig())):
            declared = declared_distinct_author(FACTORY)
        assert declared is not None
        assert declared.overlay == "product"
        assert declared.token == BOT_TOKEN

    def test_an_unresolvable_declaration_is_not_a_declared_author(self) -> None:
        with patch(_ALL_OVERLAYS, return_value=_registry(product=_ScopedConfig(scoped=""))):
            assert declared_distinct_author(FACTORY) is None

    def test_two_overlays_declaring_the_same_credential_is_not_ambiguous(self) -> None:
        registry = _registry(one=_ScopedConfig(), two=_ScopedConfig())
        with patch(_ALL_OVERLAYS, return_value=registry):
            declared = declared_distinct_author(FACTORY)
        assert declared is not None
        assert declared.token == BOT_TOKEN

    def test_two_overlays_declaring_different_credentials_refuse_to_guess(self) -> None:
        registry = _registry(one=_ScopedConfig(), two=_ScopedConfig(scoped="other-bot-token"))
        with patch(_ALL_OVERLAYS, return_value=registry), pytest.raises(AmbiguousAuthoringCredentialError) as exc:
            declared_distinct_author(FACTORY)
        assert "one" in str(exc.value)
        assert "two" in str(exc.value)

    def test_an_empty_remote_declares_nothing(self) -> None:
        with patch(_ALL_OVERLAYS, return_value=_registry(product=_ScopedConfig())):
            assert declared_distinct_author("") is None


class TestGitlabTokenForRemote:
    """The regression: the credential follows the REPO, not the ambient overlay."""

    def test_the_ambient_overlay_without_the_declaration_still_uses_the_bot(self) -> None:
        ambient = _PlainConfig()
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=ambient, product=_ScopedConfig())):
            assert gitlab_token_for_remote(ambient, FACTORY) == BOT_TOKEN

    def test_the_declaring_overlay_resolves_the_same_credential(self) -> None:
        declaring = _ScopedConfig()
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=_PlainConfig(), product=declaring)):
            assert gitlab_token_for_remote(declaring, FACTORY) == BOT_TOKEN

    def test_an_undeclared_remote_keeps_the_ambient_overlays_own_answer(self) -> None:
        ambient = _PlainConfig()
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=ambient, product=_ScopedConfig())):
            assert gitlab_token_for_remote(ambient, PRODUCT) == OWNER_TOKEN

    def test_an_unregisterable_overlay_registry_degrades_to_the_ambient_answer(self) -> None:
        ambient = _ScopedConfig()
        with patch(_ALL_OVERLAYS, side_effect=RuntimeError("no app registry")):
            assert gitlab_token_for_remote(ambient, FACTORY) == BOT_TOKEN

    def test_an_ambiguous_declaration_is_raised_not_swallowed(self) -> None:
        registry = _registry(one=_ScopedConfig(), two=_ScopedConfig(scoped="other-bot-token"))
        with patch(_ALL_OVERLAYS, return_value=registry), pytest.raises(AmbiguousAuthoringCredentialError):
            gitlab_token_for_remote(_PlainConfig(), FACTORY)


class TestAuthoringIdentityForRemote:
    """The three-valued classification, asked of the remote rather than of one overlay."""

    def test_a_remote_another_overlay_declares_reads_as_distinct(self) -> None:
        ambient = _PlainConfig()
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=ambient, product=_ScopedConfig())):
            assert authoring_identity_for_remote(FACTORY, fallback=ambient) is AuthoringIdentity.DISTINCT

    def test_an_undeclared_remote_reads_as_the_owners(self) -> None:
        ambient = _PlainConfig()
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=ambient, product=_ScopedConfig())):
            assert authoring_identity_for_remote(PRODUCT, fallback=ambient) is AuthoringIdentity.OWNER

    def test_a_declared_but_unreachable_credential_reads_as_unresolvable(self) -> None:
        ambient = _ScopedConfig(scoped="")
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=ambient)):
            assert authoring_identity_for_remote(FACTORY, fallback=ambient) is AuthoringIdentity.UNRESOLVABLE


class _Host:
    """A code host whose authenticated identity is settable — or unreadable."""

    def __init__(self, username: str = "", *, raises: bool = False) -> None:
        self._username = username
        self._raises = raises
        self.reads = 0

    def current_user(self) -> str:
        self.reads += 1
        if self._raises:
            msg = "401 Unauthorized"
            raise RuntimeError(msg)
        return self._username


class TestUnapprovableAuthorFault:
    """The pure judgement: an author the approver set relies on cannot approve its own MR."""

    def test_authoring_as_an_approver_is_a_fault_naming_both(self) -> None:
        fault = unapprovable_author_fault(remote=FACTORY, authenticated="the-owner", approvers=["the-owner"])
        assert fault is not None
        assert "the-owner" in fault.summary
        assert FACTORY in fault.summary

    def test_authoring_as_the_bot_is_no_fault(self) -> None:
        assert unapprovable_author_fault(remote=FACTORY, authenticated="the-bot", approvers=["the-owner"]) is None

    def test_the_comparison_ignores_case_and_padding(self) -> None:
        fault = unapprovable_author_fault(remote=FACTORY, authenticated=" The-Owner ", approvers=["the-owner"])
        assert fault is not None

    def test_an_unreadable_identity_is_a_fault(self) -> None:
        fault = unapprovable_author_fault(remote=FACTORY, authenticated="", approvers=["the-owner"])
        assert fault is not None
        assert "could not" in fault.summary.lower()


class TestUnapprovableAuthorRefusal:
    """The pre-create gate: refuse the MR rather than open one nobody can approve."""

    def test_a_declared_remote_authored_by_an_approver_is_refused_by_name(self) -> None:
        host = _Host("the-owner")
        with (
            patch(_ALL_OVERLAYS, return_value=_registry(ambient=_PlainConfig(), product=_ScopedConfig())),
            patch(_APPROVERS, return_value=frozenset({"the-owner"})),
        ):
            refusal = unapprovable_author_refusal(host, FACTORY)
        assert "the-owner" in refusal
        assert FACTORY in refusal

    def test_a_declared_remote_authored_by_the_bot_passes(self) -> None:
        host = _Host("the-bot")
        with (
            patch(_ALL_OVERLAYS, return_value=_registry(ambient=_PlainConfig(), product=_ScopedConfig())),
            patch(_APPROVERS, return_value=frozenset({"the-owner"})),
        ):
            assert unapprovable_author_refusal(host, FACTORY) == ""

    def test_an_undeclared_remote_is_never_gated_and_never_read(self) -> None:
        host = _Host("the-owner")
        with (
            patch(_ALL_OVERLAYS, return_value=_registry(ambient=_PlainConfig(), product=_ScopedConfig())),
            patch(_APPROVERS, return_value=frozenset({"the-owner"})),
        ):
            assert unapprovable_author_refusal(host, PRODUCT) == ""
        assert host.reads == 0

    def test_an_unreadable_identity_on_a_declared_remote_fails_closed(self) -> None:
        host = _Host(raises=True)
        with (
            patch(_ALL_OVERLAYS, return_value=_registry(ambient=_PlainConfig(), product=_ScopedConfig())),
            patch(_APPROVERS, return_value=frozenset({"the-owner"})),
        ):
            refusal = unapprovable_author_refusal(host, FACTORY)
        assert refusal
        assert FACTORY in refusal

    def test_a_declared_but_unresolvable_credential_is_left_to_the_no_host_refusal(self) -> None:
        # An unresolvable scoped credential yields an EMPTY token, so no host is built at all and
        # ``unresolvable_author_refusal`` names it. This gate never sees that lane.
        host = _Host("the-owner")
        with (
            patch(_ALL_OVERLAYS, return_value=_registry(ambient=_ScopedConfig(scoped=""))),
            patch(_APPROVERS, return_value=frozenset({"the-owner"})),
        ):
            assert unapprovable_author_refusal(host, FACTORY) == ""
        assert host.reads == 0

    def test_an_unreadable_overlay_registry_leaves_the_create_ungated(self) -> None:
        host = _Host("the-owner")
        with (
            patch(_ALL_OVERLAYS, side_effect=RuntimeError("no app registry")),
            patch(_APPROVERS, return_value=frozenset({"the-owner"})),
        ):
            assert unapprovable_author_refusal(host, FACTORY) == ""


def _git_repo_with_origin(path: Path, origin_url: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git = "/usr/bin/git"
    subprocess.run([git, "-C", str(path), "init", "-q", "-b", "main"], check=True, capture_output=True)
    subprocess.run([git, "-C", str(path), "remote", "add", "origin", origin_url], check=True, capture_output=True)
    return str(path)


class TestCodeHostForRepoAuthorsUnderTheRepoCredential:
    """End to end through the loader: the identity no longer depends on the ambient overlay."""

    def test_the_host_built_by_a_non_declaring_ambient_overlay_carries_the_bot_credential(self, tmp_path: Path) -> None:
        ambient = _PlainConfig()
        repo = _git_repo_with_origin(tmp_path / "factory", FACTORY)
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=ambient, product=_ScopedConfig())):
            host = get_code_host_for_repo(_overlay(ambient), repo)
        assert isinstance(host, GitLabCodeHost)
        assert host.client.token == BOT_TOKEN

    def test_an_undeclared_repo_still_carries_the_ambient_overlays_credential(self, tmp_path: Path) -> None:
        ambient = _PlainConfig()
        repo = _git_repo_with_origin(tmp_path / "product", PRODUCT)
        with patch(_ALL_OVERLAYS, return_value=_registry(ambient=ambient, product=_ScopedConfig())):
            host = get_code_host_for_repo(_overlay(ambient), repo)
        assert isinstance(host, GitLabCodeHost)
        assert host.client.token == OWNER_TOKEN


class _RefusingRegistry:
    """The declaring/non-declaring overlay pair, as a context manager for the host resolver."""

    def __init__(self, *, declared: bool) -> None:
        self._registry = _registry(ambient=_PlainConfig(), product=_ScopedConfig()) if declared else _registry()

    def __enter__(self) -> None:
        self._patch = patch(_ALL_OVERLAYS, return_value=self._registry)
        self._patch.start()

    def __exit__(self, *_exc: object) -> None:
        self._patch.stop()


class TestAuthorizedPrHost:
    """The one seam a PR-opening path clears its host through: the host, or the reason not to."""

    def _authorize(self, host: object, *, declared: bool, approver: str = "the-owner") -> object:
        registry = _registry(ambient=_PlainConfig(), product=_ScopedConfig()) if declared else _registry()
        with (
            patch("teatree.core.authoring_credential.git.remote_url", return_value=FACTORY),
            patch(_APPROVERS, return_value=frozenset({approver})),
            patch(_ALL_OVERLAYS, return_value=registry),
        ):
            return authorized_pr_host(host, "/tmp/checkout")

    def test_an_author_the_forge_would_bar_from_approving_is_refused_by_name(self) -> None:
        refusal = self._authorize(_Host("the-owner"), declared=True)
        assert isinstance(refusal, str)
        assert "the-owner" in refusal
        assert FACTORY in refusal

    def test_the_declared_bot_gets_the_host_back(self) -> None:
        host = _Host("the-bot")
        assert self._authorize(host, declared=True) is host

    def test_an_undeclared_repo_gets_the_host_back_even_authored_by_the_approver(self) -> None:
        host = _Host("the-owner")
        assert self._authorize(host, declared=False) is host

    def test_no_credentials_at_all_is_its_own_named_refusal(self) -> None:
        assert self._authorize(None, declared=True) == "no code host configured"


class TestShipResolvesItsHostThroughTheGate:
    """The ship path's refusal is the same one, surfaced as a structured ``RunnerResult``."""

    _SEAM = "teatree.core.runners.ship.authorized_pr_host"
    _FACTORY = "teatree.core.runners.ship.code_host_for_repo_from_overlay"

    def test_a_refusal_becomes_a_structured_ship_failure(self) -> None:
        with (
            patch(self._FACTORY, return_value=_Host("the-owner")),
            patch(self._SEAM, return_value="would be authored by 'the-owner', who is also the approver"),
        ):
            result = ShipExecutor._resolve_host("/tmp/checkout")
        assert isinstance(result, RunnerResult)
        assert result.ok is False
        assert "the-owner" in result.detail

    def test_an_accepted_host_is_returned_unchanged(self) -> None:
        host = _Host("the-bot")
        with patch(self._FACTORY, return_value=host), patch(self._SEAM, return_value=host):
            assert ShipExecutor._resolve_host("/tmp/checkout") is host

    def test_the_gate_is_handed_the_repo_path_the_ship_resolved(self) -> None:
        asked: list[str] = []
        with (
            patch(self._FACTORY, return_value=_Host("the-bot")),
            patch(self._SEAM, side_effect=lambda _host, path: asked.append(path) or "refused"),
        ):
            ShipExecutor._resolve_host("/tmp/checkout")
        assert asked == ["/tmp/checkout"]


class TestUnresolvableAuthorRefusal:
    """A declared author this venue cannot act as is named, not left as "no code host"."""

    _REMOTE = "teatree.core.authoring_credential.git.remote_url"

    def _refusal(self, config: OverlayConfig, registry: dict[str, OverlayBase], remote: str = FACTORY) -> str:
        with patch(self._REMOTE, return_value=remote), patch(_ALL_OVERLAYS, return_value=registry):
            return unresolvable_author_refusal("/tmp/checkout", overlay_config=config)

    def test_an_unreachable_declared_credential_is_named_with_its_remote_and_fix(self) -> None:
        refusal = self._refusal(_PlainConfig(), _registry(product=_ScopedConfig(scoped="")))

        assert FACTORY in refusal
        assert "Fix:" in refusal

    def test_a_resolvable_declared_credential_is_not_a_refusal(self) -> None:
        assert self._refusal(_PlainConfig(), _registry(product=_ScopedConfig())) == ""

    def test_a_remote_nobody_declares_is_not_a_refusal(self) -> None:
        assert self._refusal(_PlainConfig(), _registry(ambient=_PlainConfig()), remote=PRODUCT) == ""

    def test_a_repo_with_no_remote_is_not_a_refusal(self) -> None:
        assert self._refusal(_PlainConfig(), _registry(product=_ScopedConfig(scoped="")), remote="") == ""

    def test_an_unreadable_remote_degrades_to_the_generic_message(self) -> None:
        # One caller is the git pre-push hook, where raising aborts the very push.
        with (
            patch(self._REMOTE, side_effect=RuntimeError("git unavailable")),
            patch(_ALL_OVERLAYS, return_value=_registry(product=_ScopedConfig(scoped=""))),
        ):
            assert unresolvable_author_refusal("/tmp/checkout", overlay_config=_PlainConfig()) == ""


class TestNoHostRefusalNamesTheAuthorCause:
    """``authorized_pr_host``'s missing-host arm reports the author cause when that is the cause."""

    _OVERLAY = "teatree.core.authoring_credential.get_overlay"

    def _authorize(self, registry: dict[str, OverlayBase]) -> object:
        with (
            patch("teatree.core.authoring_credential.git.remote_url", return_value=FACTORY),
            patch(_ALL_OVERLAYS, return_value=registry),
            patch(self._OVERLAY, return_value=_overlay(_PlainConfig())),
        ):
            return authorized_pr_host(None, "/tmp/checkout")

    def test_an_unreachable_declared_credential_replaces_the_generic_message(self) -> None:
        refusal = self._authorize(_registry(product=_ScopedConfig(scoped="")))

        assert isinstance(refusal, str)
        assert refusal != "no code host configured"
        assert FACTORY in refusal

    def test_a_genuinely_unconfigured_forge_keeps_the_generic_message(self) -> None:
        assert self._authorize(_registry(product=_ScopedConfig())) == "no code host configured"

    def test_an_unresolvable_overlay_keeps_the_generic_message(self) -> None:
        with (
            patch("teatree.core.authoring_credential.git.remote_url", return_value=FACTORY),
            patch(self._OVERLAY, side_effect=RuntimeError("no overlay registered")),
        ):
            assert authorized_pr_host(None, "/tmp/checkout") == "no code host configured"
