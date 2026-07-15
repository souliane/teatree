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
    reset_backend_caches,
)
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.messaging_owner_restricted import OwnerDmOnlyError, OwnerRestrictedMessaging
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.backend_protocols import BackendResolutionError
from teatree.core.overlay import OverlayBase, OverlayConfig

_GIT = shutil.which("git") or "git"


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
    overlay = MagicMock(spec=OverlayBase)
    config = OverlayConfig()
    for key, value in config_kwargs.items():
        setattr(config, key, value)
    overlay.config = config
    return cast("OverlayBase", overlay)


def _stub_token(overlay: OverlayBase, *, github: str = "", gitlab: str = "", slack: str = "") -> None:
    overlay.config.get_github_token = lambda: github  # type: ignore[method-assign]
    overlay.config.get_gitlab_token = lambda: gitlab  # type: ignore[method-assign]
    overlay.config.get_slack_token = lambda: slack  # type: ignore[method-assign]


def test_get_code_host_returns_none_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: False)
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


def test_get_code_hosts_returns_empty_when_no_tokens_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: False)
    overlay = _build_overlay()
    _stub_token(overlay)
    assert get_code_hosts(overlay) == []


def test_get_code_hosts_explicit_choice_returns_empty_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning a platform but having no token for it surfaces as an empty list."""
    monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: False)
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


class TestOverlayScopedAmbientGithub:
    """A gh-CLI-only box builds a GitHub host for the overlay-scoped resolvers.

    ``get_github_token()`` returns ``""`` when auth lives purely in the ``gh``
    CLI login (no PAT wired into the overlay). Pre-fix, ``get_code_hosts``
    returned ``[]``, ``OverlayBackends.host`` was ``None``, and every
    host-dependent loop scanner was silently disabled. The overlay-scoped
    resolvers now mirror the ``_github_host_for_repo`` ambient carve-out: an
    empty-token ``GitHubCodeHost`` backs the logged-in ``gh`` account.
    """

    def test_get_code_hosts_builds_ambient_github_when_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        overlay = _build_overlay()
        _stub_token(overlay)
        hosts = get_code_hosts(overlay)
        assert [type(h).__name__ for h in hosts] == [GitHubCodeHost.__name__]

    def test_get_code_host_builds_ambient_github_when_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        overlay = _build_overlay()
        _stub_token(overlay)
        host = get_code_host(overlay)
        assert isinstance(host, GitHubCodeHost)

    def test_host_backend_builds_ambient_github_when_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        overlay = _build_overlay()
        _stub_token(overlay)
        assert isinstance(_host_backend(overlay, "github"), GitHubCodeHost)

    def test_no_ambient_and_no_token_stays_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: the no-auth path is unchanged when ambient gh is absent."""
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: False)
        overlay = _build_overlay()
        _stub_token(overlay)
        assert get_code_hosts(overlay) == []
        assert get_code_host(overlay) is None
        assert _host_backend(overlay, "github") is None

    def test_explicit_token_builds_single_host_not_duplicated_by_ambient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit token authors the sole host — the ambient path never duplicates it."""
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-explicit")
        hosts = get_code_hosts(overlay)
        assert [type(h).__name__ for h in hosts] == [GitHubCodeHost.__name__]
        assert cast("GitHubCodeHost", hosts[0])._token == "gh-explicit"
        assert cast("GitHubCodeHost", get_code_host(overlay))._token == "gh-explicit"

    def test_explicit_gitlab_keeps_primary_over_ambient_github(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit GitLab token outranks an ambient-only GitHub host for hosts[0]."""
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        overlay = _build_overlay()
        _stub_token(overlay, gitlab="gl-explicit")
        hosts = get_code_hosts(overlay)
        assert isinstance(hosts[0], GitLabCodeHost)
        assert [type(h).__name__ for h in hosts] == [GitLabCodeHost.__name__, GitHubCodeHost.__name__]
        # Singular resolver stays consistent: explicit gitlab wins.
        assert isinstance(get_code_host(overlay), GitLabCodeHost)

    def test_pinned_gitlab_never_builds_ambient_github(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        overlay = _build_overlay(code_host="gitlab")
        _stub_token(overlay, gitlab="gl-explicit")
        hosts = get_code_hosts(overlay)
        assert all(not isinstance(h, GitHubCodeHost) for h in hosts)
        assert not isinstance(get_code_host(overlay), GitHubCodeHost)


def test_get_messaging_default_is_noop() -> None:
    overlay = _build_overlay()
    _stub_token(overlay)
    assert isinstance(get_messaging(overlay), NoopMessagingBackend)


def test_get_messaging_returns_slack_when_chosen() -> None:
    overlay = _build_overlay(messaging_backend="slack")
    _stub_token(overlay, slack="xoxb-fake")
    assert isinstance(get_messaging(overlay), SlackBotBackend)


def test_get_messaging_full_profile_is_bare_slack() -> None:
    # The default "full" profile must NOT wrap — customer overlays post everywhere.
    overlay = _build_overlay(messaging_backend="slack", slack_scope_profile="full")
    _stub_token(overlay, slack="xoxb-fake")
    assert isinstance(get_messaging(overlay), SlackBotBackend)


def test_get_messaging_dm_only_wraps_in_owner_restricted() -> None:
    overlay = _build_overlay(
        messaging_backend="slack",
        slack_scope_profile="dm_only",
        slack_user_id="U-owner",
        slack_dm_channel_id="D-owner",
    )
    _stub_token(overlay, slack="xoxb-fake")
    backend = get_messaging(overlay)
    assert isinstance(backend, OwnerRestrictedMessaging)
    assert isinstance(backend._inner, SlackBotBackend)
    # The owner identity is threaded so the guard can recognise the self-DM.
    assert backend._dm_channel_id == "D-owner"
    assert backend._user_id == "U-owner"


def test_get_messaging_dm_only_refuses_non_owner_channel() -> None:
    overlay = _build_overlay(
        messaging_backend="slack",
        slack_scope_profile="dm_only",
        slack_user_id="U-owner",
        slack_dm_channel_id="D-owner",
    )
    _stub_token(overlay, slack="xoxb-fake")
    backend = get_messaging(overlay)
    with pytest.raises(OwnerDmOnlyError):
        backend.post_message(channel="C-public", text="leak")


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


def test_get_code_host_for_url_returns_none_when_no_matching_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: False)
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

    def test_github_hosted_repo_resolves_github_even_when_gitlab_token_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-tok", gitlab="gl-tok")
        # The configured GitHub token can push here — keep it (no ambient probe,
        # hermetic: never shell a real ``gh api``).
        monkeypatch.setattr("teatree.backends.loader.gh_can_push", lambda _slug, *, token="": True)
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


class TestGetCodeHostForRepoGithubAmbientAuth:
    """A tokenless overlay falls back to ``gh``'s own ambient auth (#2946).

    ``_run_gh`` already inherits the parent environment (and thus ``gh``'s
    logged-in account) when no explicit token is passed — ``_host_backend``
    used to short-circuit to ``None`` before that fallback ever got a
    chance to run. GitLab's REST transport has no equivalent ambient-auth
    path (see ``GitLabHTTPClient.get_json``/``post_json``), so it keeps
    raising on an empty token.
    """

    def test_falls_back_to_ambient_auth_when_no_token_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay = _build_overlay()
        _stub_token(overlay)  # no GitHub, no GitLab token configured
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        repo = _git_repo_with_origin(tmp_path / "gh-ambient", "git@github.com:souliane/teatree.git")

        result = get_code_host_for_repo(overlay, repo)

        assert isinstance(result, GitHubCodeHost)

    def test_configured_token_that_can_push_is_used_without_ambient_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay = _build_overlay()
        _stub_token(overlay, github="gh-tok")
        # The configured token CAN push to this repo, so it authors the PR and
        # the ambient account is never consulted (a working token costs one
        # ``repos/{slug}`` push probe and nothing more).
        monkeypatch.setattr("teatree.backends.loader.gh_can_push", lambda _slug, *, token="": True)
        monkeypatch.setattr(
            "teatree.backends.loader.gh_ambient_auth_available",
            lambda: (_ for _ in ()).throw(AssertionError("ambient must not be probed when the token can push")),
        )
        repo = _git_repo_with_origin(tmp_path / "gh-tok", "git@github.com:souliane/teatree.git")

        result = get_code_host_for_repo(overlay, repo)

        assert isinstance(result, GitHubCodeHost)
        assert result._token == "gh-tok"

    def test_non_collaborator_token_falls_back_to_ambient_collaborator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The configured token is NOT a collaborator on this repo (its
        # ``createPullRequest`` fails "must be a collaborator"), but the ambient
        # ``gh`` CLI account IS. The collaborator identity must author the PR.
        overlay = _build_overlay()
        _stub_token(overlay, github="bot-token")

        def fake_can_push(_slug: str, *, token: str = "") -> bool:
            # Only the ambient (empty-token) identity can push here.
            return token == ""

        monkeypatch.setattr("teatree.backends.loader.gh_can_push", fake_can_push)
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        repo = _git_repo_with_origin(tmp_path / "gh-collab", "git@github.com:souliane/teatree.git")

        result = get_code_host_for_repo(overlay, repo)

        assert isinstance(result, GitHubCodeHost)
        # The COLLABORATOR identity (ambient gh account, token="") authors the PR,
        # NOT the configured non-collaborator token. Reverting the fix returns the
        # bot token here and re-triggers the "must be a collaborator" abort.
        assert result._token == ""

    def test_non_collaborator_token_kept_when_ambient_also_cannot_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Neither the configured token nor the ambient account can push — never
        # silently switch identity; keep the configured token so the real error
        # surfaces rather than guessing an identity that also cannot create.
        overlay = _build_overlay()
        _stub_token(overlay, github="bot-token")
        monkeypatch.setattr("teatree.backends.loader.gh_can_push", lambda _slug, *, token="": False)
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: True)
        repo = _git_repo_with_origin(tmp_path / "gh-nopush", "git@github.com:souliane/teatree.git")

        result = get_code_host_for_repo(overlay, repo)

        assert isinstance(result, GitHubCodeHost)
        assert result._token == "bot-token"

    def test_raises_when_no_token_and_ambient_auth_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay = _build_overlay()
        _stub_token(overlay)
        monkeypatch.setattr("teatree.backends.loader.gh_ambient_auth_available", lambda: False)
        repo = _git_repo_with_origin(tmp_path / "gh-noauth", "git@github.com:souliane/teatree.git")

        with pytest.raises(BackendResolutionError, match="github"):
            get_code_host_for_repo(overlay, repo)
