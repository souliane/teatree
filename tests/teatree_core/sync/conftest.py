"""Package-local fixtures for the sync test package.

Preserves the autouse overlay-cache reset that wrapped every test in the
former monolithic ``tests/teatree_core/test_sync.py`` (souliane/teatree#443).
"""

import pytest

from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState


@pytest.fixture(autouse=True)
def _route_sync_overlay_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(overlay, *, credential: str, overlay_name: str = "") -> ForgeTokenResolution:
        token = overlay.config.get_github_token()
        state = ForgeTokenState.TOKEN if token else ForgeTokenState.UNSET
        return ForgeTokenResolution(credential, overlay_name or "test", state, token=token)

    monkeypatch.setattr("teatree.forge_credentials.resolve_overlay_token", resolve)
