"""The core → backends builder/loader inversion registry (#1922)."""

import pytest

from teatree.core import backend_registry


class TestBackendProviderRegistry:
    def test_backends_ready_registers_the_real_provider(self) -> None:
        """``BackendsConfig.ready()`` ran at django.setup() — the real provider resolves."""
        from teatree.backends.backend_provider import SlackBackendProvider  # noqa: PLC0415

        assert isinstance(backend_registry.get_backend_provider(), SlackBackendProvider)

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
