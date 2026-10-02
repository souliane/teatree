"""Pin the overlay set a query-plan measurement runs against.

Every settings surface renders one column per registered overlay and resolves each
column's tier chain, so an exact query pin is a function of how many overlays the
*venue* happens to have installed. Upstream CI installs the core package alone and
measures two scopes; a fork that registers its own overlay measures three, and the
same page reads the same way at a different number. Pinning the dimension here keeps
the assertion about the plan — flat in the number of SETTINGS — rather than about
which packages the runner installed.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

CORE_OVERLAY = "t3-teatree"


@contextmanager
def core_overlay_only() -> Iterator[None]:
    """Resolve settings columns as a core-only install does: global plus one overlay."""
    with patch("teatree.dash.settings_editor.get_all_overlays", return_value=[CORE_OVERLAY]):
        yield
