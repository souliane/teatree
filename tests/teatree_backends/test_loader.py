"""Tests for the backend loader."""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from teatree.backends.github import GitHubCodeHost
from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab.ci import GitLabCIService
from teatree.backends.loader import (
    _host_backend,
    get_ci_service,
    get_code_host,
    get_code_host_for_repo,
    get_code_host_for_url,
    get_code_hosts,
    get_messaging,
    pr_is_merged_or_closed,
    pr_open_state,
    reset_backend_caches,
)
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.routing import OwnerDmOnlyError
from teatree.config.credential_pass_key import PassKeyResolution, PassKeySource
from teatree.core.backend_protocols import BackendResolutionError, PrOpenState
from teatree.core.backend_registry import UnknownSlackScopeProfileError
from teatree.core.overlay import OverlayBase, OverlayConfig
from teatree.forge_credentials import (
    ForgeCredentialRequest,
    ForgeCredentialTarget,
    ForgeTokenResolution,
    ForgeTokenState,
)

_GIT = shutil.which("git") or "git"
_ACTIVE_OVERLAY: OverlayBase | None = None


@pytest.fixture(autouse=True)
def _central_forge_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(request: ForgeCredentialRequest) -> ForgeTokenResolution:
        overlay = request.target if request.target_kind is ForgeCredentialTarget.OVERLAY else _ACTIVE_OVERLAY
        token = ""
        if overlay is not None:
            token = (
                overlay.config.get_github_token()
                if request.credential == "github_token"
                else overlay.config.get_gitlab_token()
            )
        return ForgeTokenResolution(
            request.credential,
            request.overlay_name or "test",
            ForgeTokenState.TOKEN if token else ForgeTokenState.UNSET,
            token=token,
        )

    monkeypatch.setattr("teatree.forge_credentials._provider", resolve)


def _git_repo_with_origin(path: Path, origin_url: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run([_GIT, "-C", str(path), "init", "-q", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        [_GIT, "-C", str(path), "remote", "add", "origin", origin_url],
        check=True,
        capture_output=True,
    )
    return str(path)


def setup_function() -> None:
    reset_backend_caches()


def teardown_function() -> None:
    reset_backend_caches()


def _build_overlay(**config_kwargs: object) -> OverlayBase:
    global _ACTIVE_OVERLAY  # noqa: PLW0603 - test-local owner registry
    overlay = MagicMock(spec=OverlayBase)
    config = _StubTokenConfig()
    for key, value in config_kwargs.items():
        setattr(config, key, value)
    overlay.config = config
    _ACTIVE_OVERLAY = cast("OverlayBase", overlay)
    return _ACTIVE_OVERLAY


class _StubTokenConfig(OverlayConfig):
    """An ``OverlayConfig`` whose token reads are settable.

    A real subclass overriding the three readers, not a rebound bound method, so
    the double is type-checked like production code. ``_build_overlay`` always
    creates this class, so ``_stub_token`` mutates the SAME config instance the
    caller's ``config_kwargs`` were applied to rather than replacing it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._github = self._gitlab = self._slack = ""

    def set_tokens(self, *, github: str = "", gitlab: str = "", slack: str = "") -> None:
        self._github, self._gitlab, self._slack = github, gitlab, slack

    def get_github_token(self) -> str:
        return self._github

    def get_gitlab_token(self) -> str:
        return self._gitlab

    def get_slack_token(self) -> str:
        return self._slack

    def resolve_pass_key(self, name: str) -> PassKeyResolution:
        token = self._github if name == "github_token" else self._gitlab if name == "gitlab_token" else ""
        source = PassKeySource.DECLARED_DEFAULT if token else PassKeySource.UNSET
        return PassKeyResolution(f"{name}_pass_key", token, source)


def _stub_token(overlay: OverlayBase, *, github: str = "", gitlab: str = "", slack: str = "") -> None:
    cast("_StubTokenConfig", overlay.config).set_tokens(github=github, gitlab=gitlab, slack=slack)


def test_get_code_host_returns_none_when_no_token() -> None:
    overlay = _build_overlay()
    _stub_token(overlay)
    assert get_code_host(overlay) is None


def test_get_code_host_returns_github_when_explicit_choice() -> None:
    overlay = _build_overlay(code_host="github")
    _stub_token(overlay, github="gh-test-token")
    assert isinstance(get_code_host(overlay), GitHubCodeHost)


def test_get_code_host_returns_gitlab_when_explicit_choice() -> None:
    overlay = _build_overlay(code_host="gitlab")
    _stub_token(overlay, gitlab="gl-test-token")
    assert isinstance(get_code_host(overlay), GitLabCodeHost)


def test_get_code_host_falls_back_to_token_when_choice_unset() -> None:
    overlay = _build_overlay()
    _stub_token(overlay, gitlab="gl-test-token")
    assert isinstance(get_code_host(overlay), GitLabCodeHost)


def test_get_code_host_raises_on_unknown_choice() -> None:
    overlay = _build_overlay(code_host="bogus")
    _stub_token(overlay)
    with pytest.raises(ValueError, match="Unknown code_host"):
        get_code_host(overlay)


def test_get_code_hosts_returns_both_when_both_tokens_set() -> None:
    """An overlay that opts into auto-pick gets both code hosts (#976)."""
    overlay = _build_overlay()
    _stub_token(overlay, github="gh-test", gitlab="gl-test")
    hosts = get_code_hosts(overlay)
    types = sorted(type(h).__name__ for h in hosts)
    assert types == [GitHubCodeHost.__name__, GitLabCodeHost.__name__]


def test_get_code_hosts_honours_explicit_github_choice() -> None:
    """An overlay that pins ``code_host = github`` still gets one host even when both PATs are set."""
    overlay = _build_overlay(code_host="github")
    _stub_token(overlay, github="gh-test", gitlab="gl-test")
    hosts = get_code_hosts(overlay)
    assert [type(h).__name__ for h in hosts] == [GitHubCodeHost.__name__]


def test_get_code_hosts_honours_explicit_gitlab_choice() -> None:
    overlay = _build_overlay(code_host="gitlab")
    _stub_token(overlay, github="gh-test", gitlab="gl-test")
    hosts = get_code_hosts(overlay)
    assert [type(h).__name__ for h in hosts] == [GitLabCodeHost.__name__]


def test_get_code_hosts_returns_empty_when_no_tokens_resolve() -> None:
    overlay = _build_overlay()
    _stub_token(overlay)
    assert get_code_hosts(overlay) == []


def test_get_code_hosts_explicit_choice_returns_empty_without_token() -> None:
    """Pinning a platform but having no token for it surfaces as an empty list."""
    overlay = _build_overlay(code_host="github")
    _stub_token(overlay)
    assert get_code_hosts(overlay) == []
    overlay = _build_overlay(code_host="gitlab")
    _stub_token(overlay)
    assert get_code_hosts(overlay) == []


def test_get_code_hosts_raises_on_unknown_choice() -> None:
    overlay = _build_overlay(code_host="bogus")
    _stub_token(overlay)
    with pytest.raises(ValueError, match="Unknown code_host"):
        get_code_hosts(overlay)


class TestOverlayScopedGithub:
    def test_hostile_ambient_login_never_builds_a_tokenless_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "hostile")
        monkeypatch.setenv("GITHUB_TOKEN", "also-hostile")
        overlay = _build_overlay()
        assert get_code_hosts(overlay) == []
        assert get_code_host(overlay) is None
        assert _host_backend(overlay, "github", "git@github.com:org/repo.git") is None

    def test_routed_token_wins_over_hostile_ambient_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "hostile")
        overlay = _build_overlay()
        _stub_token(overlay, github="routed-owner")
        hosts = get_code_hosts(overlay)
        assert [type(h).__name__ for h in hosts] == [GitHubCodeHost.__name__]
        assert cast("GitHubCodeHost", hosts[0])._token == "routed-owner"


def test_get_messaging_default_is_noop() -> None:
    overlay = _build_overlay()
    _stub_token(overlay)
    assert isinstance(get_messaging(overlay), NoopMessagingBackend)


def test_get_messaging_returns_slack_when_chosen() -> None:
    overlay = _build_overlay(messaging_backend="slack")
    _stub_token(overlay, slack="xoxb-fake")
    assert isinstance(get_messaging(overlay), SlackBotBackend)


def test_get_messaging_full_profile_leaves_owner_dm_only_off() -> None:
    # The default "full" profile must NOT restrict — customer overlays post everywhere.
    overlay = _build_overlay(messaging_backend="slack", slack_scope_profile="full")
    _stub_token(overlay, slack="xoxb-fake")
    backend = get_messaging(overlay)
    assert isinstance(backend, SlackBotBackend)
    assert backend._owner_dm_only is False


def test_get_messaging_dm_only_sets_owner_dm_only() -> None:
    overlay = _build_overlay(
        messaging_backend="slack",
        slack_scope_profile="dm_only",
        slack_user_id="U-owner",
        slack_dm_channel_id="D-owner",
    )
    _stub_token(overlay, slack="xoxb-fake")
    backend = get_messaging(overlay)
    assert isinstance(backend, SlackBotBackend)
    assert backend._owner_dm_only is True


def test_get_messaging_unknown_profile_refuses_to_build() -> None:
    overlay = _build_overlay(messaging_backend="slack", slack_scope_profile="dm-only", slack_user_id="U-owner")
    _stub_token(overlay, slack="xoxb-fake")
    with pytest.raises(UnknownSlackScopeProfileError, match="slack_scope_profile"):
        get_messaging(overlay)


def test_get_messaging_dm_only_refuses_non_owner_channel() -> None:
    overlay = _build_overlay(
        messaging_backend="slack",
        slack_scope_profile="dm_only",
        slack_user_id="U-owner",
        slack_dm_channel_id="D-owner",
    )
    _stub_token(overlay, slack="xoxb-fake")
    backend = get_messaging(overlay)
    # The guard raises before any HTTP call for a non-owner destination.
    with pytest.raises(OwnerDmOnlyError):
        backend.post_message(channel="C-public", text="leak")


class TestCredentiallessSlackNeverPretendsToBeSlack:
    """A Slack backend with no bot token must not be handed out (#3334 class).

    ``read_pass`` returns ``""`` on any store failure — a locked gpg-agent under a
    long-lived daemon is the common one — so ``resolve_messaging_tokens`` can hand
    back an empty bot token. Constructing ``SlackBotBackend`` anyway produced a
    backend whose every call short-circuits to ``{}`` BEFORE any HTTP, which the
    notify layer then reported as ``conversations.open ok:false`` and "Slack post
    returned no message ts" — Slack-shaped diagnoses for a credential fault that
    never reached Slack. That sent the owner hunting a scope problem that did not
    exist. Degrading to noop is the truthful answer, and it keeps the documented
    contract that a messaging credential fault never wedges merges/CI/PR sweeps.
    """

    def test_empty_bot_token_degrades_to_noop(self) -> None:
        overlay = _build_overlay(messaging_backend="slack")
        _stub_token(overlay, slack="")
        assert isinstance(get_messaging(overlay), NoopMessagingBackend)

    def test_the_degradation_is_logged_with_the_unresolvable_ref(self, caplog: pytest.LogCaptureFixture) -> None:
        overlay = _build_overlay(messaging_backend="slack", slack_token_ref="secrets/example-overlay/slack")
        _stub_token(overlay, slack="")
        with caplog.at_level(logging.WARNING, logger="teatree.backends.loader"):
            get_messaging(overlay)
        assert "secrets/example-overlay/slack" in caplog.text

    def test_a_real_bot_token_is_untouched(self) -> None:
        overlay = _build_overlay(messaging_backend="slack")
        _stub_token(overlay, slack="xoxb-fake")
        assert isinstance(get_messaging(overlay), SlackBotBackend)


def test_get_messaging_raises_on_unknown_choice() -> None:
    overlay = _build_overlay(messaging_backend="bogus")
    _stub_token(overlay)
    with pytest.raises(ValueError, match="Unknown messaging_backend"):
        get_messaging(overlay)


def test_get_messaging_resolves_user_token_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """``user_token_ref`` is resolved via ``pass`` and threaded into ``SlackBotBackend``.

    Slack-Connect channels reject ``xoxb`` reactions; routing them through
    the human's ``xoxp`` token is the workaround.  The loader must read the
    ref from ``pass`` and hand the resolved secret to the backend.
    """
    pass_lookups: dict[str, str] = {
        "ref/bot-bot": "xoxb-resolved",
        "ref/bot-app": "xapp-resolved",
        "slack/user-oauth": "xoxp-resolved",
    }

    def fake_read_pass(key: str) -> str:
        return pass_lookups.get(key, "")

    monkeypatch.setattr("teatree.utils.secrets.read_pass", fake_read_pass)

    overlay = _build_overlay(
        messaging_backend="slack",
        slack_token_ref="ref/bot",
        user_token_ref="slack/user-oauth",
    )
    backend = get_messaging(overlay)

    assert isinstance(backend, SlackBotBackend)
    assert backend.user_token == "xoxp-resolved"


def test_get_messaging_degrades_malformed_user_token_to_bot_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An ``xoxb-`` value in the user slot must NOT crash the loop — degrade to bot-only.

    The #1285 follow-up: a Slack-only credential typo (``pass`` holds an
    ``xoxb-…`` where the ``xoxp-…`` user token belongs) must never wedge
    merges, CI, or PR sweeps. ``get_messaging`` is a loop construction
    path, so it builds a working bot-only backend and warns rather than
    raising ``TokenSlotMismatchError``.
    """
    pass_lookups: dict[str, str] = {
        "ref/bot-bot": "xoxb-resolved",
        "ref/bot-app": "xapp-resolved",
        "slack/user-oauth": "xoxb-mistakenly-pasted-into-user-slot",
    }
    monkeypatch.setattr("teatree.utils.secrets.read_pass", lambda key: pass_lookups.get(key, ""))

    overlay = _build_overlay(
        messaging_backend="slack",
        slack_token_ref="ref/bot",
        user_token_ref="slack/user-oauth",
    )
    with caplog.at_level(logging.WARNING):
        backend = get_messaging(overlay)

    assert isinstance(backend, SlackBotBackend)
    assert backend.user_token == ""
    assert "t3 setup slack-user-token" in caplog.text


def test_get_messaging_user_token_absent_when_ref_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``user_token_ref`` the backend keeps an empty user token."""

    # Per-slot prefixes — #1285 validates them at construction.
    def fake_read_pass(key: str) -> str:
        return {"ref/bot-bot": "xoxb-resolved", "ref/bot-app": "xapp-resolved"}.get(key, "")

    monkeypatch.setattr("teatree.utils.secrets.read_pass", fake_read_pass)

    overlay = _build_overlay(messaging_backend="slack", slack_token_ref="ref/bot")
    backend = get_messaging(overlay)

    assert isinstance(backend, SlackBotBackend)
    assert backend.user_token == ""


def test_get_ci_service_returns_none_when_no_token() -> None:
    assert get_ci_service() is None


def test_get_ci_service_returns_gitlab_when_token_present() -> None:
    result = get_ci_service(gitlab_token="gl-test-token")
    assert isinstance(result, GitLabCIService)


def test_reset_backend_caches_clears_ci() -> None:
    reset_backend_caches()
    assert get_ci_service() is None


def test_get_ci_service_cache_survives_a_second_overlay() -> None:
    """Two overlays' CI services coexist in the cache — no maxsize=1 thrash.

    A user running two overlays (each with its own GitLab token/url) resolves a
    CI service per overlay. With ``maxsize=1`` the second overlay evicted the
    first, so re-resolving the first rebuilt it on every alternating call. The
    cache must hold both: re-resolving the first overlay after the second is a
    hit (same instance), not a rebuild.
    """
    reset_backend_caches()
    first = get_ci_service(gitlab_token="tok-a", gitlab_url="https://a.example/api/v4")
    get_ci_service(gitlab_token="tok-b", gitlab_url="https://b.example/api/v4")
    first_again = get_ci_service(gitlab_token="tok-a", gitlab_url="https://a.example/api/v4")
    assert first is first_again


def test_get_code_host_for_url_returns_github_for_github_url() -> None:
    overlay = _build_overlay()
    _stub_token(overlay, github="gh-tok", gitlab="gl-tok")
    result = get_code_host_for_url(overlay, "https://github.com/org/repo/issues/1")
    assert isinstance(result, GitHubCodeHost)


def test_get_code_host_for_url_returns_gitlab_for_gitlab_url() -> None:
    overlay = _build_overlay()
    _stub_token(overlay, github="gh-tok", gitlab="gl-tok")
    result = get_code_host_for_url(overlay, "https://gitlab.com/group/repo/-/issues/42")
    assert isinstance(result, GitLabCodeHost)


def test_get_code_host_for_url_falls_back_to_default_for_unknown_domain() -> None:
    overlay = _build_overlay()
    _stub_token(overlay, github="gh-tok")
    result = get_code_host_for_url(overlay, "https://unknown.example.com/issues/1")
    assert isinstance(result, GitHubCodeHost)


def test_get_code_host_for_url_returns_none_when_no_matching_token() -> None:
    overlay = _build_overlay()
    _stub_token(overlay)
    assert get_code_host_for_url(overlay, "https://github.com/org/repo/issues/1") is None


class TestGetCodeHostForRepo:
    """#2025: resolve the forge from the repo's actual origin remote host.

    The ship path picked the backend by token-presence precedence
    (GitHub first when both PATs are set), so a GitLab-hosted repo on an
    overlay carrying both PATs ran ``gh pr create`` against a GitLab
    remote and failed with ``Could not resolve to a Repository``. The
    forge must derive from where the repo actually lives.
    """

    def test_gitlab_hosted_repo_resolves_gitlab_even_when_github_token_set(self, tmp_path: Path) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-tok", gitlab="gl-tok")
        repo = _git_repo_with_origin(tmp_path / "gl", "git@gitlab.com:group/repo.git")
        assert isinstance(get_code_host_for_repo(overlay, repo), GitLabCodeHost)

    def test_github_hosted_repo_resolves_github_even_when_gitlab_token_set(self, tmp_path: Path) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-tok", gitlab="gl-tok")
        repo = _git_repo_with_origin(tmp_path / "gh", "git@github.com:souliane/teatree.git")
        assert isinstance(get_code_host_for_repo(overlay, repo), GitHubCodeHost)

    def test_https_gitlab_remote_resolves_gitlab(self, tmp_path: Path) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-tok", gitlab="gl-tok")
        repo = _git_repo_with_origin(tmp_path / "gl2", "https://gitlab.com/group/repo.git")
        assert isinstance(get_code_host_for_repo(overlay, repo), GitLabCodeHost)

    def test_self_hosted_gitlab_remote_resolves_gitlab(self, tmp_path: Path) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-tok", gitlab="gl-tok")
        repo = _git_repo_with_origin(tmp_path / "gl3", "git@gitlab.example.com:group/repo.git")
        assert isinstance(get_code_host_for_repo(overlay, repo), GitLabCodeHost)

    def test_raises_structured_error_when_host_backend_has_no_token(self, tmp_path: Path) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-tok")  # no GitLab token
        repo = _git_repo_with_origin(tmp_path / "gl4", "git@gitlab.com:group/repo.git")
        with pytest.raises(BackendResolutionError, match="gitlab"):
            get_code_host_for_repo(overlay, repo)

    def test_no_origin_remote_falls_back_to_default_resolution(self, tmp_path: Path) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, gitlab="gl-tok")
        path = tmp_path / "no-origin"
        path.mkdir()
        subprocess.run([_GIT, "-C", str(path), "init", "-q", "-b", "main"], check=True, capture_output=True)
        assert isinstance(get_code_host_for_repo(overlay, str(path)), GitLabCodeHost)


class TestGitlabTokenIsScopedToTheRemote:
    """The GitLab host is built with the token the overlay picks FOR THAT remote.

    An overlay may author on one repo under a different identity (the bot that
    lets a human approve the MR); the resolver must hand it the origin remote so
    it can decide, and must not silently fall back to the overlay-wide token.
    """

    @staticmethod
    def _scoped_overlay(privileged_slug: str) -> OverlayBase:
        overlay = _build_overlay()
        _stub_token(overlay, gitlab="ordinary-token")

        def scoped(remote: str) -> str:
            return "scoped-token" if privileged_slug in remote else "ordinary-token"

        overlay.config.get_gitlab_token_for_remote = scoped  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
        return overlay

    def test_repo_resolution_passes_the_origin_remote_through(self, tmp_path: Path) -> None:
        overlay = self._scoped_overlay("group/privileged")
        repo = _git_repo_with_origin(tmp_path / "privileged", "git@gitlab.com:group/privileged.git")
        host = cast("GitLabCodeHost", get_code_host_for_repo(overlay, repo))
        assert host.client.token == "scoped-token"

    def test_an_unprivileged_repo_still_gets_the_ordinary_token(self, tmp_path: Path) -> None:
        overlay = self._scoped_overlay("group/privileged")
        repo = _git_repo_with_origin(tmp_path / "ordinary", "git@gitlab.com:group/ordinary.git")
        host = cast("GitLabCodeHost", get_code_host_for_repo(overlay, repo))
        assert host.client.token == "ordinary-token"

    def test_host_backend_takes_the_remote(self) -> None:
        overlay = self._scoped_overlay("group/privileged")
        backend = cast("GitLabCodeHost", _host_backend(overlay, "gitlab", "git@gitlab.com:group/privileged.git"))
        assert backend.client.token == "scoped-token"


class TestGetCodeHostForRepoGithubRouting:
    def test_hostile_ambient_login_never_replaces_an_unset_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "hostile")
        monkeypatch.setenv("GITHUB_TOKEN", "also-hostile")
        overlay = _build_overlay()
        repo = _git_repo_with_origin(tmp_path / "gh-noauth", "git@github.com:souliane/teatree.git")

        with pytest.raises(BackendResolutionError, match="github_token_pass_key"):
            get_code_host_for_repo(overlay, repo)

    def test_routed_owner_identity_is_kept_for_collaborator_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "hostile-collaborator")
        overlay = _build_overlay()
        _stub_token(overlay, github="routed-owner")
        repo = _git_repo_with_origin(tmp_path / "gh-owner", "git@github.com:souliane/teatree.git")

        result = get_code_host_for_repo(overlay, repo)

        assert isinstance(result, GitHubCodeHost)
        assert result._token == "routed-owner"


class _OpenStateHost:
    """A code host whose PR open-state read returns *state*, or raises."""

    def __init__(self, state: PrOpenState | None, *, raises: bool = False) -> None:
        self._state = state
        self._raises = raises

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        if self._raises:
            msg = f"forge unreachable for {pr_url}"
            raise RuntimeError(msg)
        return cast("PrOpenState", self._state)


class TestPrOpenState:
    """MERGED must be distinguishable from CLOSED, and every failure collapses to UNKNOWN.

    The board reconcile advances a landed ticket and resolves an abandoned one, so the
    two verdicts drive opposite transitions — a seam that merely answered "merged or
    closed?" could not tell them apart. Every indeterminate case is UNKNOWN so a failed
    read can never be mistaken for a definite verdict.
    """

    def _patch(self, monkeypatch: pytest.MonkeyPatch, host: object | None) -> None:
        monkeypatch.setattr("teatree.core.overlay_loader.get_overlay_for_url", lambda _url: _build_overlay())
        monkeypatch.setattr("teatree.backends.loader.get_code_host_for_url", lambda _overlay, _url: host)

    def test_merged_and_closed_are_distinct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, _OpenStateHost(PrOpenState.MERGED))
        assert pr_open_state("https://x/pull/1") is PrOpenState.MERGED
        self._patch(monkeypatch, _OpenStateHost(PrOpenState.CLOSED))
        assert pr_open_state("https://x/pull/1") is PrOpenState.CLOSED

    def test_open_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, _OpenStateHost(PrOpenState.OPEN))
        assert pr_open_state("https://x/pull/1") is PrOpenState.OPEN

    def test_blank_url_is_unknown(self) -> None:
        assert pr_open_state("") is PrOpenState.UNKNOWN

    def test_no_host_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, None)
        assert pr_open_state("https://x/pull/1") is PrOpenState.UNKNOWN

    def test_probe_failure_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, _OpenStateHost(None, raises=True))
        assert pr_open_state("https://x/pull/1") is PrOpenState.UNKNOWN


class TestPrIsMergedOrClosed:
    """The fail-OPEN predicate built on the state read — only a DEFINITE verdict is True."""

    def _patch(self, monkeypatch: pytest.MonkeyPatch, state: PrOpenState) -> None:
        monkeypatch.setattr("teatree.backends.loader.pr_open_state", lambda _url: state)

    def test_true_for_merged_and_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, PrOpenState.MERGED)
        assert pr_is_merged_or_closed("https://x/pull/1") is True
        self._patch(monkeypatch, PrOpenState.CLOSED)
        assert pr_is_merged_or_closed("https://x/pull/1") is True

    def test_false_for_open_and_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, PrOpenState.OPEN)
        assert pr_is_merged_or_closed("https://x/pull/1") is False
        self._patch(monkeypatch, PrOpenState.UNKNOWN)
        assert pr_is_merged_or_closed("https://x/pull/1") is False
