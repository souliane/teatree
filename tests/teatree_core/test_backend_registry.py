"""The core → backends builder/loader inversion registry (#1922)."""

import re

import pytest

from teatree.core import backend_registry
from teatree.types import SharePointRemoteSpec

_SHAREPOINT_SPEC = SharePointRemoteSpec(
    remote="sp:",
    root="r",
    config_path="c",
    password_command="p",
    site_url="s",
    library_path="l",
)


@pytest.mark.parametrize(("value", "expected"), [("", "full"), ("full", "full"), ("dm_only", "dm_only")])
def test_unset_or_known_slack_scope_profile_parses(value: str, expected: str) -> None:
    assert backend_registry.parse_slack_scope_profile(value) == expected


@pytest.mark.parametrize("value", ["dm-only", "DM_ONLY", "bogus"])
def test_unknown_slack_scope_profile_raises(value: str) -> None:
    with pytest.raises(backend_registry.UnknownSlackScopeProfileError, match="slack_scope_profile"):
        backend_registry.parse_slack_scope_profile(value)


@pytest.mark.parametrize("value", [True, False, ["dm_only"], 0, {"profile": "dm_only"}, None])
def test_wrong_type_slack_scope_profile_raises(value: object) -> None:
    with pytest.raises(backend_registry.UnknownSlackScopeProfileError, match=re.escape(repr(value))):
        backend_registry.parse_slack_scope_profile(value)


def test_unknown_slack_scope_profile_error_is_value_error() -> None:
    assert issubclass(backend_registry.UnknownSlackScopeProfileError, ValueError)


class TestBackendProviderRegistry:
    def test_backends_ready_registers_the_real_provider(self) -> None:
        """``BackendsConfig.ready()`` ran at django.setup() — the real provider resolves."""
        from teatree.backends.backend_provider import ConcreteBackendProvider  # noqa: PLC0415

        assert isinstance(backend_registry.get_backend_provider(), ConcreteBackendProvider)

    def test_unconfigured_provider_builds_nothing(self) -> None:
        """Fail-SAFE: with no provider registered, builds degrade to None/empty (no crash)."""
        original = backend_registry._provider
        backend_registry._provider = None
        try:
            provider = backend_registry.get_backend_provider()
            assert provider.get_code_host(object()) is None
            assert provider.get_code_host_for_repo(object(), "/tmp/repo") is None
            assert provider.get_code_hosts(object()) == []
            assert provider.get_messaging(object()) is None
            assert provider.get_ci_service(gitlab_token="t", gitlab_url="u") is None
            assert provider.build_sync_backends() == []
            assert provider.build_notion_client(token="t") is None
            assert provider.build_sentry_client(token="t", org="o", base_url="u") is None
            assert provider.build_sharepoint_client(_SHAREPOINT_SPEC) is None
            provider.reset_caches()
        finally:
            backend_registry.register_backend_provider(original)

    def test_unconfigured_provider_raises_on_concrete_build(self) -> None:
        """A concrete build with no backends app is a misconfiguration, not a silent no-op."""
        original = backend_registry._provider
        backend_registry._provider = None
        try:
            provider = backend_registry.get_backend_provider()
            with pytest.raises(RuntimeError, match="no backend provider registered"):
                provider.build_github_host(token="t")
        finally:
            backend_registry.register_backend_provider(original)

    def test_unconfigured_review_read_is_not_ok(self) -> None:
        """Fail-SAFE: an unconfigured review-history read reports not-ok with no matches."""
        original = backend_registry._provider
        backend_registry._provider = None
        try:
            spec = backend_registry.ReviewSearchSpec(
                token="t",
                channel_id="C1",
                channel_name="rev",
                pr_urls=["https://example/1"],
                max_pages=1,
                oldest_ts="0",
                timeout=1.0,
            )
            read = backend_registry.get_backend_provider().read_recent_review_matches(spec)
            assert read.ok is False
            assert read.matches == []
        finally:
            backend_registry.register_backend_provider(original)

    def test_unconfigured_thread_activity_is_not_ok(self) -> None:
        """Fail-SAFE: an unconfigured thread-activity read reports not-ok, thread absent."""
        original = backend_registry._provider
        backend_registry._provider = None
        try:
            spec = backend_registry.ThreadActivitySpec(
                token="t",
                channel_id="C1",
                thread_ts="1700000000.000100",
                timeout=1.0,
            )
            read = backend_registry.get_backend_provider().read_thread_activity(spec)
            assert read.ok is False
            assert read.exists is False
            assert read.parent_ts == ""
            assert read.latest_reply_ts == ""
            assert read.has_reaction is False
        finally:
            backend_registry.register_backend_provider(original)
