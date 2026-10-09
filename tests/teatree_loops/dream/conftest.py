"""Installation values shared by dream tests that publish through real gates."""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.hooks import _repo_visibility
from tests._send_gate import allow_forge_repos


@pytest.fixture(autouse=True)
def _configured_dream_publication(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_banned_term_registry: None,
) -> None:
    """Give Django-backed dream flows the registry and forge destinations of an installation."""
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "t3-data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    if request.cls is not None and issubclass(request.cls, TestCase):
        allow_forge_repos("souliane/teatree", "o/factory")
