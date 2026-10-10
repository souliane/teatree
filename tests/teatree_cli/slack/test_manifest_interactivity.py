"""The app manifest turns Interactivity on, so the buttons on an owner question can be tapped (#4990)."""

import pytest

from teatree.cli.slack.manifest import build_manifest, manifests_equivalent


@pytest.mark.parametrize("profile", ["full", "dm_only"])
def test_every_scope_profile_enables_interactivity(profile: str) -> None:
    manifest = build_manifest(overlay_name="acme", scope_profile=profile)

    assert manifest["settings"]["interactivity"] == {"is_enabled": True}


@pytest.mark.parametrize("profile", ["full", "dm_only"])
def test_a_manifest_with_interactivity_off_is_different(profile: str) -> None:
    wanted = build_manifest(overlay_name="acme", scope_profile=profile)
    current = build_manifest(overlay_name="acme", scope_profile=profile)
    current["settings"]["interactivity"] = {"is_enabled": False}

    assert not manifests_equivalent(current, wanted)
    assert manifests_equivalent(wanted, build_manifest(overlay_name="acme", scope_profile=profile))


def test_a_manifest_that_never_mentioned_interactivity_is_different() -> None:
    wanted = build_manifest(overlay_name="acme")
    current = build_manifest(overlay_name="acme")
    del current["settings"]["interactivity"]

    assert not manifests_equivalent(current, wanted)
