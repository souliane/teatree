"""The GitHub App manifest is deterministic, minimal, and carries no secrets (#4795)."""

from teatree.backends.github.app_manifest import (
    CAPABILITY_FOR_PERMISSION,
    DEFAULT_EVENTS,
    DEFAULT_PERMISSIONS,
    build_manifest,
)


class TestManifestDeterminism:
    def test_same_inputs_produce_byte_identical_manifests(self) -> None:
        first = build_manifest(
            name="teatree-souliane", url="https://example.com", webhook_url="https://example.com/hooks/github/"
        )
        second = build_manifest(
            name="teatree-souliane", url="https://example.com", webhook_url="https://example.com/hooks/github/"
        )
        assert first == second

    def test_manifest_carries_no_secret_fields(self) -> None:
        manifest = build_manifest(
            name="teatree-souliane", url="https://example.com", webhook_url="https://example.com/hooks/github/"
        )
        serialised = str(manifest).lower()
        for forbidden in ("private_key", "webhook_secret", "client_secret", "pem"):
            assert forbidden not in serialised

    def test_manifest_names_the_webhook_url(self) -> None:
        manifest = build_manifest(name="x", url="https://example.com", webhook_url="https://example.com/hooks/github/")
        assert manifest["hook_attributes"]["url"] == "https://example.com/hooks/github/"

    def test_manifest_is_not_public(self) -> None:
        manifest = build_manifest(name="x", url="https://example.com", webhook_url="https://example.com/hooks/github/")
        assert manifest["public"] is False


class TestPermissionCapabilityMapping:
    def test_every_requested_permission_maps_to_a_named_capability(self) -> None:
        assert set(DEFAULT_PERMISSIONS) == set(CAPABILITY_FOR_PERMISSION)
        for permission, capability in CAPABILITY_FOR_PERMISSION.items():
            assert isinstance(capability, str)
            assert capability, f"{permission} has no named capability"

    def test_manifest_default_permissions_match_the_capability_map(self) -> None:
        manifest = build_manifest(name="x", url="https://example.com", webhook_url="https://example.com/hooks/github/")
        assert manifest["default_permissions"] == DEFAULT_PERMISSIONS

    def test_manifest_default_events_are_exactly_the_shipped_set(self) -> None:
        manifest = build_manifest(name="x", url="https://example.com", webhook_url="https://example.com/hooks/github/")
        assert set(manifest["default_events"]) == set(DEFAULT_EVENTS)
