"""The ``E2ERepo`` value object — an external repo carrying Playwright E2E tests.

Split out of ``config/settings.py`` so that giant settings-resolution module stays
under the module-health LOC cap; this dataclass is a standalone value object with
no dependency on the settings machinery. Re-exported from ``teatree.config``.
"""

from dataclasses import dataclass


@dataclass
class E2ERepo:
    """An external git repository containing Playwright E2E tests."""

    name: str
    url: str
    branch: str
    e2e_dir: str = "e2e"
