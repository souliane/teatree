"""Package-local fixtures for the sync test package.

Preserves the autouse overlay-cache reset that wrapped every test in the
former monolithic ``tests/teatree_core/test_sync.py`` (souliane/teatree#443).
"""

from collections.abc import Iterator

import pytest

from teatree.core.overlay_loader import reset_overlay_cache


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()
