"""E2E test fixtures — start a live Django dev server for Playwright.

Supports parallel execution via pytest-xdist: each worker gets its own SQLite
DB and Django dev server, so tests run with zero shared state.

Run with:
    t3 teatree e2e project
"""

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

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

# Repo-root-relative artifact dirs — mounted writable into the e2e Docker
# container (see ``dev/docker-compose.yml``). Created lazily so a developer
# running ``uv run pytest`` outside Docker also gets a populated folder.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOGS_DIR = _REPO_ROOT / "e2e" / ".logs"
_VIDEOS_DIR = _REPO_ROOT / "e2e" / ".videos"


def pytest_configure(config: pytest.Config) -> None:
    """Override DJANGO_SETTINGS_MODULE before pytest-django initializes Django."""
    os.environ["DJANGO_SETTINGS_MODULE"] = "e2e.settings"
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


_DISABLE_ANIMATIONS_CSS = (
    "*,*::before,*::after{"
    "animation-duration:0s!important;animation-delay:0s!important;"
    "transition-duration:0s!important;transition-delay:0s!important;"
    "caret-color:transparent!important;"
    "}"
)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, object]) -> dict[str, object]:
    """Fix viewport (1280x720 for README rendering), reduce motion, record video.

    Pixel-stable screenshots require both a fixed viewport and aggressive
    animation suppression — per-call ``animations="disabled"`` only catches
    CSS animations Playwright knows about, not JS-driven transitions or
    hover/loading states. See [#275](https://github.com/souliane/teatree/issues/275)
    (credits @m13v). The ``_disable_animations_init`` fixture below injects a
    global stylesheet on every navigation to cover the rest.

    ``record_video_dir`` enables Playwright video recording for every test;
    the ``_video_retain_on_failure`` fixture deletes the video for passing
    tests so artifact size only grows with failures.
    """
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "reduced_motion": "reduce",
        "record_video_dir": str(_VIDEOS_DIR),
        "record_video_size": {"width": 1280, "height": 720},
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


@pytest.fixture(autouse=True)
def _video_retain_on_failure(request: pytest.FixtureRequest, page) -> Iterator[None]:
    """Mimic Playwright's ``--video=retain-on-failure`` mode.

    pytest-playwright doesn't expose an equivalent CLI flag, and recording
    every video would balloon CI artifact size. Strategy: always record (via
    ``browser_context_args``), then on test exit delete the video iff the
    test passed. Test outcome is stashed by ``pytest_runtest_makereport``.
    """
    yield
    rep = getattr(request.node, "rep_call", None)
    video_path: Path | None = None
    try:
        video = page.video
        if video is not None:
            page.context.close()  # flush video to disk
            raw = video.path()
            if raw:
                video_path = Path(raw)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        video_path = None
    if rep is not None and rep.passed and video_path is not None and video_path.exists():
        video_path.unlink(missing_ok=True)


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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:
    """Surface the server log path on failure + stash outcome for video fixture."""
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_call = rep  # ty: ignore[unresolved-attribute]
    if rep.failed:
        log_path = getattr(item.session, "_e2e_server_log_path", None)
        if log_path:
            sys.stderr.write(
                f"\n[E2E] Server log for failure ({item.nodeid}, {rep.when}): {log_path}\n",
            )


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
def e2e_server(django_db_setup, request: pytest.FixtureRequest) -> Iterator[str]:
    """Start ASGI server (uvicorn) on a free port, yield URL.

    Using ASGI instead of WSGI (runserver) so SSE streaming connections
    are handled properly — async generators are cancelled on client
    disconnect instead of leaking threads.

    Server stdout+stderr are tee'd to ``e2e/.logs/server-<ISO>.log`` rather
    than discarded — failures in CI are otherwise opaque. The path is
    surfaced via ``pytest_runtest_makereport`` whenever a test fails.
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
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    log_path = _LOGS_DIR / f"server-{timestamp}-{worker}.log"
    request.session._e2e_server_log_path = log_path  # noqa: SLF001  # ty: ignore[unresolved-attribute]
    sys.stderr.write(f"E2E server log: {log_path}\n")
    log_handle = log_path.open("wb")
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
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
    )

    try:
        _wait_for_server(url)
    except TimeoutError:
        log_handle.close()
        proc.kill()
        proc.wait(timeout=3)
        sys.stderr.write(f"\n[E2E] Server failed to start. Log: {log_path}\n")
        raise
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    log_handle.close()


@pytest.fixture(autouse=True)
def _seed_data(e2e_server: str, django_db_blocker) -> Iterator[None]:
    """Seed DB before each test, flush after."""
    from django.core.cache import cache

    from teatree.core.models import Session, Task, Ticket, Worktree
    from teatree.core.sync import PENDING_REVIEWS_CACHE_KEY

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
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/demo-frontend",
            branch="feat-42-fe",
            state="ready",
            db_name="wt_42_demo_fe",
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
        from teatree.core.models import TaskAttempt

        frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        TaskAttempt.objects.update(started_at=frozen, ended_at=frozen)

        # Seed pending_reviews via the Django cache the panel builder reads.
        # Using FileBasedCache (see ``e2e/settings.py``) so the live ASGI
        # subprocess sees the same payload as the test process.
        cache.set(
            PENDING_REVIEWS_CACHE_KEY,
            [
                {
                    "url": "https://gitlab.example.com/org/backend/-/merge_requests/505",
                    "title": "feat(reports): nightly export pipeline",
                    "repo": "backend",
                    "iid": "505",
                    "author": "alice",
                    "draft": "false",
                    "updated_at": "2026-01-01T08:00:00Z",
                },
                {
                    "url": "https://gitlab.example.com/org/frontend/-/merge_requests/612",
                    "title": "fix(login): handle SSO redirect race",
                    "repo": "frontend",
                    "iid": "612",
                    "author": "bob",
                    "draft": "true",
                    "updated_at": "2026-01-02T09:30:00Z",
                },
            ],
            timeout=None,
        )

    yield

    with django_db_blocker.unblock():
        Ticket.objects.all().delete()
        cache.clear()


@pytest.fixture(scope="session", autouse=True)
def _purge_logs_videos_on_session_end() -> Iterator[None]:
    """Drop empty log/video dirs at the end of a clean session.

    Failed runs leave the artifacts in place — ``_video_retain_on_failure``
    already deletes per-test videos for passing tests, so anything still on
    disk after a green run is empty boilerplate.
    """
    yield
    for path in (_LOGS_DIR, _VIDEOS_DIR):
        if path.is_dir() and not any(path.iterdir()):
            shutil.rmtree(path, ignore_errors=True)


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
