"""E2E test fixtures — start a live Django dev server for Playwright.

Supports parallel execution via pytest-xdist: each worker gets its own SQLite
DB and Django dev server, so tests run with zero shared state.

Run with:
    t3 teatree e2e project
"""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC

import httpx
import patchy
import pytest
from pytest_playwright_visual import plugin as _ppv_plugin

os.environ["DJANGO_SETTINGS_MODULE"] = "e2e.settings"
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"
# Isolate active-session discovery from any real Claude sessions on the host.
os.environ["TEATREE_CLAUDE_SESSIONS_DIR"] = "/nonexistent-e2e-sessions"
# Disable panel cache so seeded data appears immediately on each test.
os.environ["TEATREE_PANEL_CACHE_TTL"] = "0"
# Pin the dashboard header's git SHA/branch so pixel-diff stays stable across
# commits — read by `teatree.core.views.dashboard.DashboardView.get`. Set at
# module level so `subprocess.Popen(env=os.environ)` inherits them.
os.environ["TEATREE_E2E_GIT_SHA"] = "abc1234"
os.environ["TEATREE_E2E_GIT_BRANCH"] = "e2e-branch"


def pytest_configure(config: pytest.Config) -> None:
    """Override DJANGO_SETTINGS_MODULE before pytest-django initializes Django."""
    os.environ["DJANGO_SETTINGS_MODULE"] = "e2e.settings"


_DISABLE_ANIMATIONS_CSS = (
    "*,*::before,*::after{"
    "animation-duration:0s!important;animation-delay:0s!important;"
    "transition-duration:0s!important;transition-delay:0s!important;"
    "caret-color:transparent!important;"
    "}"
)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, object]) -> dict[str, object]:
    """Fix viewport (1280x720 for README rendering) and reduce motion.

    Pixel-stable screenshots require both a fixed viewport and aggressive
    animation suppression — per-call ``animations="disabled"`` only catches
    CSS animations Playwright knows about, not JS-driven transitions or
    hover/loading states. See [#275](https://github.com/souliane/teatree/issues/275)
    (credits @m13v). The ``_disable_animations_init`` fixture below injects a
    global stylesheet on every navigation to cover the rest.
    """
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "reduced_motion": "reduce",
    }


@pytest.fixture(autouse=True)
def _disable_animations_init(page) -> None:
    """Inject the no-animation stylesheet on every navigation."""
    script = (
        "(()=>{const s=document.createElement('style');"
        f"s.textContent={_DISABLE_ANIMATIONS_CSS!r};"
        "(document.head||document.documentElement).appendChild(s);})();"
    )
    page.add_init_script(script)


# pytest-playwright-visual's `assert_snapshot` hard-fails on any single-pixel
# mismatch, which makes full-page screenshots impossible to keep stable across
# host architectures (font antialiasing varies between Apple-Silicon Docker,
# x86_64 Docker, and bare-metal Ubuntu CI runners). Patch the strict
# `if mismatch == 0:` check to allow up to 0.5% of pixels to differ. See
# [#275](https://github.com/souliane/teatree/issues/275).
#
# patchy reads source via `inspect.getsource` (which includes `@pytest.fixture`)
# and re-execs it; the decorator would re-wrap the result into a
# `FixtureFunctionDefinition` with no `__code__`, breaking patchy's swap.
# Neutralize `pytest.fixture` to identity for the duration of the patch so the
# exec returns a plain function. We patch `__wrapped__` (the underlying
# function); the original `FixtureFunctionDefinition` keeps wrapping it.
_orig_fixture = pytest.fixture
pytest.fixture = lambda *a, **_k: a[0] if a and callable(a[0]) else (lambda f: f)  # ty: ignore[invalid-assignment]
try:
    # editorconfig-checker-disable
    patchy.patch(
        _ppv_plugin.assert_snapshot.__wrapped__,  # ty: ignore[unresolved-attribute]
        """\
@@ -30,7 +30,7 @@
         img_b = Image.open(file)
         img_diff = Image.new("RGBA", img_a.size)
         mismatch = pixelmatch(img_a, img_b, img_diff, threshold=threshold, fail_fast=fail_fast)
-        if mismatch == 0:
+        if mismatch / (img_a.size[0] * img_a.size[1]) <= 0.005:
             return
         else:
             # Create new test_results folder
""",
    )
    # editorconfig-checker-enable
finally:
    pytest.fixture = _orig_fixture


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Exempt fixture setup/teardown from pytest-timeout for E2E tests.

    Under xdist each worker starts its own Playwright browser + Django server
    inside session-scoped fixtures.  The global ``timeout = 10`` (from
    pyproject.toml) would kill setup before Playwright finishes launching.
    Adding ``func_only=True`` keeps the timeout on the test body only.
    """
    mark = pytest.mark.timeout(func_only=True)
    for item in items:
        item.add_marker(mark)


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    """Prevent pytest-django from renaming the DB to a test_ prefix."""


@pytest.fixture(scope="session")
def django_db_setup(django_db_modify_db_settings, django_db_blocker):
    """Run migrations against the per-worker E2E SQLite file.

    Deletes any stale DB file first so reused TEATREE_E2E_DB_DIR dirs
    (from killed processes or inherited env vars) start clean.
    """
    from e2e.settings import E2E_DB_PATH

    E2E_DB_PATH.unlink(missing_ok=True)

    # Force Django to use the E2E database regardless of which settings module
    # was loaded (pyproject.toml may have set a different one before conftest).
    from django.conf import settings as django_settings

    django_settings.DATABASES["default"]["ENGINE"] = "django.db.backends.sqlite3"
    django_settings.DATABASES["default"]["NAME"] = str(E2E_DB_PATH)

    with django_db_blocker.unblock():
        from django.core.management import call_command

        call_command("migrate", "--run-syncdb", "--no-input", verbosity=0)


@pytest.fixture(scope="session")
def e2e_server(django_db_setup) -> Iterator[str]:
    """Start ASGI server (uvicorn) on a free port, yield URL.

    Using ASGI instead of WSGI (runserver) so SSE streaming connections
    are handled properly — async generators are cancelled on client
    disconnect instead of leaking threads.
    """
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    from django.conf import settings as django_settings

    from e2e.settings import _DB_DIR, E2E_DB_PATH

    # Debug: ensure test process and server subprocess use the same DB
    actual_db = django_settings.DATABASES["default"]["NAME"]
    sys.stderr.write(f"E2E debug: test DB={actual_db}, E2E_DB_PATH={E2E_DB_PATH}, DB_DIR={_DB_DIR}\n")

    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "e2e.settings",
        "TEATREE_E2E_DB_DIR": str(_DB_DIR),
    }
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "e2e.asgi:application",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    _wait_for_server(url)
    yield url
    proc.terminate()
    try:
        _, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate(timeout=3)
    if stderr:
        sys.stderr.write(f"E2E server stderr:\n{stderr.decode()[-1000:]}\n")


@pytest.fixture(autouse=True)
def _seed_data(e2e_server: str, django_db_blocker) -> Iterator[None]:
    """Seed DB before each test, flush after."""
    from teatree.core.models import Session, Task, Ticket, Worktree

    with django_db_blocker.unblock():
        # Ticket with MR data
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/42",
            variant="demo",
            state="started",
            extra={
                "issue_title": "Fix the login bug",
                "labels": ["bug", "priority::high"],
                "mrs": {
                    "backend!100": {
                        "url": "https://gitlab.example.com/org/backend/-/merge_requests/100",
                        "repo": "backend",
                        "iid": 100,
                        "title": "fix(auth): resolve login timeout",
                        "draft": False,
                        "pipeline_status": "success",
                        "pipeline_url": "https://gitlab.example.com/org/backend/-/pipelines/999",
                        "approvals": {"count": 1, "required": 1},
                    },
                    "frontend!200": {
                        "url": "https://gitlab.example.com/org/frontend/-/merge_requests/200",
                        "repo": "frontend",
                        "iid": 200,
                        "title": "fix(auth): update login form",
                        "draft": True,
                        "pipeline_status": "failed",
                        "pipeline_url": "https://gitlab.example.com/org/frontend/-/pipelines/998",
                        "approvals": {"count": 0, "required": 1},
                    },
                },
            },
        )
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/demo-backend",
            branch="feat-42",
            state="provisioned",
            db_name="wt_42_demo",
        )
        session = Session.objects.create(ticket=ticket, agent_id="e2e-agent")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target="headless",
            execution_reason="Automated code review",
            phase="reviewing",
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target="interactive",
            execution_reason="Needs manual verification",
            phase="testing",
        )

        # Ticket without MRs (covers {% empty %} branch)
        ticket2 = Ticket.objects.create(
            issue_url="https://example.com/issues/99",
            variant="acme",
            state="scoped",
        )
        Session.objects.create(ticket=ticket2, agent_id="e2e-agent-2")

        # Pin TaskAttempt timestamps so dashboard screenshots are reproducible.
        # The headless task above triggers the immediate-backend signal which
        # creates a TaskAttempt with `ended_at=now()` — that microsecond-precision
        # timestamp is rendered in the Sessions panel and would change every run.
        from datetime import datetime

        from teatree.core.models import TaskAttempt

        frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        TaskAttempt.objects.update(started_at=frozen, ended_at=frozen)

    yield

    with django_db_blocker.unblock():
        Ticket.objects.all().delete()


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, *, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.3)
    msg = f"Server at {url} did not start within {timeout}s"
    raise TimeoutError(msg)
