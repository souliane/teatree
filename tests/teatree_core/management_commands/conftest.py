"""Package-local fixtures for management command tests.

Preserves the autouse overlay-cache reset that wrapped every test in the
former monolithic module.
"""

from collections.abc import Iterator

import pytest

from teatree.core.overlay_loader import reset_overlay_cache

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


@pytest.fixture(autouse=True)
def _clear_overlay() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()
