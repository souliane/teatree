"""Fixtures for the Notion backend suite."""

import pytest

from tests.teatree_backends.notion._fake_notion import FakeNotion, install_fake_notion


@pytest.fixture
def notion(monkeypatch: pytest.MonkeyPatch) -> FakeNotion:
    """A live-ish Notion the real client talks to over ``httpx.MockTransport``."""
    return install_fake_notion(monkeypatch)
