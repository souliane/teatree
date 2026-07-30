"""Launch arguments for the /dash/ e2e lane's browser.

Kept out of ``conftest.py`` so tests can import it without executing conftest's
import-time side effects (it sets ``DJANGO_ALLOW_ASYNC_UNSAFE`` for the sync
Playwright + sync Django combination, which must not leak into other lanes).
"""

BROWSER_EXECUTABLE_ENV = "E2E_CHROMIUM_EXECUTABLE"


def browser_launch_overrides(executable: str | None) -> dict[str, object]:
    """Launch-arg overrides for an externally-provided chromium, or none when unset.

    Playwright ships browser builds per distro and has none for every host it can
    otherwise run on — ``playwright install`` refuses outright on an unrecognised
    platform, so the lane is unrunnable there even though a perfectly good chromium
    is installed. Pointing Playwright at that binary is the supported escape.

    ``--no-sandbox`` is required because a distro chromium's SUID sandbox helper is
    not installed at the path Playwright's own build uses; the other two flags avoid
    a GPU probe and a shared-memory sizing assumption that headless containers and
    confined desktop packages both break on.
    """
    if not executable:
        return {}
    return {
        "executable_path": executable,
        "args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    }
